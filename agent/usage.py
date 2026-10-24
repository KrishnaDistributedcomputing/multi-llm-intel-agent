"""Durable provider-reported token accounting, without prompts or answers."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)
UsageScope = Literal["chat", "verification"]
UsageFilter = Literal["all", "chat", "verification"]


class UsageStoreError(RuntimeError):
    """Usage could not be read or durably recorded."""


class CloudLimitError(RuntimeError):
    """A cloud request was rejected before contacting the provider."""


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def normalize_total(self) -> "TokenUsage":
        if self.prompt_tokens is not None and self.completion_tokens is not None:
            known_total = self.prompt_tokens + self.completion_tokens
            if self.total_tokens is None:
                self.total_tokens = known_total
            elif self.total_tokens < known_total:
                raise ValueError("Reported total is smaller than input plus output tokens")
        return self


class UsageStore:
    def __init__(self, path: Path) -> None:
        try:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            with self.transaction() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_calls (
                        id INTEGER PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        model TEXT NOT NULL,
                        scope TEXT NOT NULL CHECK(scope IN ('chat', 'verification')),
                        status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed')),
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        total_tokens INTEGER
                    )
                """)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS calls_scope_model ON model_calls(scope, model)"
                )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS cloud_reservations (
                        id INTEGER PRIMARY KEY,
                        reserved_at REAL NOT NULL,
                        utc_day TEXT NOT NULL,
                        model TEXT NOT NULL,
                        microusd INTEGER NOT NULL CHECK(microusd > 0)
                    )
                """)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cloud_day ON cloud_reservations(utc_day)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cloud_time ON cloud_reservations(reserved_at)"
                )
        except (sqlite3.Error, OSError) as exc:
            logger.error("Usage database initialization failed (%s)", type(exc).__name__)
            raise UsageStoreError("Usage database initialization failed") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.connection:
                yield self.connection
        except sqlite3.Error as exc:
            logger.error("Usage storage operation failed (%s)", type(exc).__name__)
            raise UsageStoreError("Usage storage is unavailable") from exc

    def start(self, model: str, scope: UsageScope) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO model_calls(started_at,model,scope,status) VALUES (?,?,?,'pending')",
                (datetime.now(UTC).isoformat(), model, scope),
            )
            if cursor.lastrowid is None:
                raise UsageStoreError("Usage record ID was not returned")
            return cursor.lastrowid

    def reserve_cloud(
        self, model: str, microusd: int, daily_limit: int, calls_per_minute: int,
        *, now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        day = timestamp.astimezone(UTC).date().isoformat()
        with self.transaction() as connection:
            # Lock before reading so separate processes cannot overspend the shared allowance.
            connection.execute("BEGIN IMMEDIATE")
            spent = connection.execute(
                "SELECT COALESCE(SUM(microusd),0) FROM cloud_reservations WHERE utc_day=?",
                (day,),
            ).fetchone()[0]
            if spent + microusd > daily_limit:
                raise CloudLimitError(
                    "Shared daily cloud allowance exhausted; choose a local model "
                    "or wait until the next UTC day."
                )
            recent = connection.execute(
                "SELECT COUNT(*) FROM cloud_reservations WHERE reserved_at>?",
                (timestamp.timestamp() - 60,),
            ).fetchone()[0]
            if recent >= calls_per_minute:
                raise CloudLimitError(
                    "Shared cloud rate limit reached; wait one minute or choose a local model."
                )
            connection.execute(
                "INSERT INTO cloud_reservations(reserved_at,utc_day,model,microusd) "
                "VALUES (?,?,?,?)",
                (timestamp.timestamp(), day, model, microusd),
            )

    def cloud_budget(self, daily_limit: int) -> dict[str, object]:
        day = datetime.now(UTC).date().isoformat()
        with self.transaction() as connection:
            reserved = connection.execute(
                "SELECT COALESCE(SUM(microusd),0) FROM cloud_reservations WHERE utc_day=?",
                (day,),
            ).fetchone()[0]
        return {
            "utc_day": day,
            "daily_limit_usd": daily_limit / 1_000_000,
            "reserved_usd": reserved / 1_000_000,
            "remaining_usd": max(0, daily_limit - reserved) / 1_000_000,
        }

    def finish(
        self, call_id: int, usage: TokenUsage | None, status: Literal["completed", "failed"]
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE model_calls SET status=?,prompt_tokens=?,completion_tokens=?,
                   total_tokens=? WHERE id=? AND status='pending'""",
                (
                    status,
                    usage.prompt_tokens if usage else None,
                    usage.completion_tokens if usage else None,
                    usage.total_tokens if usage else None,
                    call_id,
                ),
            )
            if cursor.rowcount != 1:
                logger.error("Usage record finalization did not match a pending call")
                raise UsageStoreError("Usage record could not be finalized")

    def summary(self, scope: UsageFilter = "all") -> dict[str, object]:
        where = "" if scope == "all" else " WHERE scope=?"
        parameters = () if scope == "all" else (scope,)
        columns = """
            COUNT(*) AS calls,
            COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0) AS completion_tokens,
            COALESCE(SUM(total_tokens),0) AS total_tokens,
            COALESCE(SUM(status='failed'),0) AS failed_calls,
            COALESCE(SUM(status='pending'),0) AS pending_calls,
            COALESCE(SUM(prompt_tokens IS NULL OR completion_tokens IS NULL
                         OR total_tokens IS NULL),0) AS unreported_calls
        """
        with self.transaction() as connection:
            totals = dict(
                connection.execute(
                    f"SELECT {columns} FROM model_calls{where}", parameters
                ).fetchone()
            )
            by_model = [
                dict(row)
                for row in connection.execute(
                    f"SELECT model,{columns} FROM model_calls{where} GROUP BY model ORDER BY model",
                    parameters,
                )
            ]
            recent = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM model_calls{where} ORDER BY id DESC LIMIT 20", parameters
                )
            ]
        return {"scope": scope, "totals": totals, "by_model": by_model, "recent": recent}

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error as exc:
            logger.error("Usage database close failed (%s)", type(exc).__name__)
            raise UsageStoreError("Usage database could not close cleanly") from exc
