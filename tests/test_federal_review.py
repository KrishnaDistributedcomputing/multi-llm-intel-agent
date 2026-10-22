"""Synthetic-only evidence for the locked federal review boundary."""

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import Message, Scope
from test_agent import ROOT, TEST_AGENT_KEY, catalog, settings

from agent.config import REVIEW_NOTICE
from agent.main import FederalReviewBoundary, create_app
from gateway.start import build_config

SYNTHETIC_DOCUMENT = (
    "SYNTHETIC TEST DATA ONLY. Fictional internal planning note. "
    "Project Cedar training exercise: 12 mock attendees, Monday at 09:00 UTC. "
    "No actual agency, personnel, operational, CUI, or classified information."
)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/agent"),
        ("POST", "/ui-api/agent"),
        ("GET", "/models"),
        ("GET", "/ui-api/models"),
        ("GET", "/usage"),
        ("GET", "/ui-api/usage"),
        ("POST", "/documents"),
        ("POST", "/ui-api/documents"),
        ("POST", "/unknown-future-route"),
        ("GET", "/agent/"),
        ("POST", "/healthz"),
    ],
)
def test_review_denies_processing_even_with_valid_token(
    method: str, path: str, authenticated: bool, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {"host": "localhost:8001", "origin": "http://localhost:8001"}
    if authenticated:
        headers["authorization"] = f"Bearer {TEST_AGENT_KEY}"
    with TestClient(create_app(settings(deployment_profile="federal-review"))) as client:
        response = client.request(
            method,
            path,
            headers=headers,
            json={"model": "local-qwen3-small", "message": SYNTHETIC_DOCUMENT},
        )
        assert response.status_code == 403
        assert response.json()["processing_enabled"] is False
        assert response.json()["detail"] == REVIEW_NOTICE
        assert SYNTHETIC_DOCUMENT not in response.text
        assert SYNTHETIC_DOCUMENT not in caplog.text
        assert TEST_AGENT_KEY not in caplog.text
        assert "body was not read" in caplog.text
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]


def test_review_starts_without_runtime_storage_client_or_verification(tmp_path: Path) -> None:
    database = tmp_path / "not-created.sqlite3"
    with (
        patch("agent.main.Runtime", side_effect=AssertionError("Runtime must not start")),
        patch("agent.main.UsageStore", side_effect=AssertionError("Storage must not open")),
        patch("agent.main.httpx.AsyncClient", side_effect=AssertionError("No outbound client")),
    ):
        app = create_app(
            settings(deployment_profile="federal-review", usage_db_path=str(database))
        )
        with TestClient(app) as client:
            assert not hasattr(app.state, "runtime")
            status = client.get("/")
            assert status.status_code == 200
            assert status.json()["authorization"] == "not_authorized"
            assert status.json()["processing_enabled"] is False
            assert "document-file" not in status.text
            assert "script" not in status.text
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
            assert client.post("/agent", content=SYNTHETIC_DOCUMENT).status_code == 403
        assert not database.exists()


def test_review_boundary_never_receives_body_and_closes_websockets() -> None:
    async def run() -> None:
        messages: list[Message] = []

        async def forbidden_receive() -> Message:
            raise AssertionError("Request content must not be read")

        async def forbidden_app(scope: Scope, receive: object, send: object) -> None:
            raise AssertionError("Processing handlers must not be reached")

        async def send(message: Message) -> None:
            messages.append(message)

        boundary = FederalReviewBoundary(forbidden_app)
        for path in ("/agent", "/ui-api/agent", "/documents"):
            messages.clear()
            await boundary(
                {"type": "http", "method": "POST", "path": path},
                forbidden_receive,
                send,
            )
            assert messages[0]["status"] == 403
        messages.clear()
        await boundary({"type": "websocket", "path": "/agent"}, forbidden_receive, send)
        assert messages == [{"type": "websocket.close", "code": 1008}]

    asyncio.run(run())


def test_review_rejects_cloud_and_gateway_and_unknown_profile() -> None:
    with pytest.raises(ValidationError, match="Cloud processing is forbidden"):
        settings(deployment_profile="federal-review", allow_cloud=True)
    with pytest.raises(ValidationError):
        settings(deployment_profile="federal-review-approved")
    with pytest.raises(ValueError, match="cannot run"):
        build_config(settings(deployment_profile="federal-review"), catalog())
    assert settings().deployment_profile == "development"


def test_review_does_not_echo_input_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(settings(deployment_profile="federal-review"))) as client:
            response = client.post(
                "/agent?document=" + SYNTHETIC_DOCUMENT,
                headers={"x-document-name": SYNTHETIC_DOCUMENT},
                content=SYNTHETIC_DOCUMENT * 1000,
            )
    assert response.status_code == 403
    assert SYNTHETIC_DOCUMENT not in caplog.text
    assert SYNTHETIC_DOCUMENT not in response.text


def test_review_compose_is_separate_and_has_no_inference_services_or_data_mounts() -> None:
    manifest = yaml.safe_load((ROOT / "compose.review.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "intel-agent-federal-review"
    assert set(manifest["services"]) == {"agent"}
    agent = manifest["services"]["agent"]
    assert agent["environment"]["DEPLOYMENT_PROFILE"] == "federal-review"
    assert agent["environment"]["ALLOW_CLOUD"] == "false"
    assert not agent.get("ports")
    assert not agent.get("network_mode")
    assert not agent.get("volumes")
    assert agent["read_only"] is True
    assert agent["cap_drop"] == ["ALL"]
    assert agent["security_opt"] == ["no-new-privileges:true"]
    assert manifest["networks"]["review"]["internal"] is True
