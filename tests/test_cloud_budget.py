"""Cloud allowances fail closed, persist, and include probes and failed calls."""

import asyncio
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent import TEST_AGENT_KEY, call, catalog, reply, settings

from agent.config import Catalog, Route
from agent.main import create_app
from agent.runtime import AgentError, Runtime, ToolChoice, initial_messages
from agent.usage import CloudLimitError, UsageStore, UsageStoreError


def cloud_route() -> Route:
    return Route(
        id="azure-test", label="Azure test", kind="cloud", model="azure/test",
        key_env="AZURE_API_KEY", enabled=True,
        input_usd_per_million=0.1, output_usd_per_million=0.4,
    )


@pytest.mark.parametrize("provider", ["azure", "openai", "local"])
def test_azure_disables_parallel_tools_without_changing_other_providers(provider: str) -> None:
    async def run() -> None:
        route = catalog().models[0] if provider == "local" else cloud_route().model_copy(
            update={"model": f"{provider}/test"},
        )
        answer = '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia"}'

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["tool_choice"] == "auto"
            assert len(body["tools"]) == 2
            if provider == "azure":
                assert body["parallel_tool_calls"] is False
            else:
                assert "parallel_tool_calls" not in body
            return reply(answer)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(allow_cloud=True), Catalog(
                models=[catalog().models[0], route] if provider != "local" else [route],
            ), client)
            try:
                result = await runtime.conversation(route, "Convert the synthetic ticket to JSON")
                assert result == answer
            finally:
                runtime.usage.close()

    asyncio.run(run())


@pytest.mark.parametrize("unsolicited_tool", [False, True])
def test_text_only_omits_tools_and_rejects_unsolicited_calls(unsolicited_tool: bool) -> None:
    async def run() -> None:
        route = cloud_route()
        answer = '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia"}'

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert not {"tools", "tool_choice", "parallel_tool_calls"} & body.keys()
            return reply(None, [call("shell", "{}")]) if unsolicited_tool else reply(answer)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(allow_cloud=True), Catalog(
                models=[catalog().models[0], route],
            ), client)
            try:
                if unsolicited_tool:
                    with pytest.raises(AgentError, match="text-only request"):
                        await runtime.conversation(route, "Synthetic JSON", tool_choice="none")
                else:
                    assert await runtime.conversation(
                        route, "Synthetic JSON", tool_choice="none",
                    ) == answer
                assert runtime.usage.summary()["totals"]["calls"] == 1
                assert runtime.usage.cloud_budget(runtime.cloud_daily_limit)["reserved_usd"] > 0
            finally:
                runtime.usage.close()

    asyncio.run(run())


@pytest.mark.parametrize("path", ["/agent", "/ui-api/agent"])
def test_request_tool_choice_is_validated_and_forwarded(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[ToolChoice] = []

    async def run(
        self: Runtime, model: str, prompt: str, *, tool_choice: ToolChoice = "auto",
    ) -> str:
        received.append(tool_choice)
        return '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia"}'

    monkeypatch.setattr(Runtime, "run", run)
    headers = (
        {"host": "localhost", "origin": "http://localhost"}
        if path.startswith("/ui-api") else {"Authorization": f"Bearer {TEST_AGENT_KEY}"}
    )
    with TestClient(create_app(settings(), background_verify=False)) as client:
        for choice in (None, "none", "required"):
            body = {"model": "local-qwen3", "message": "Synthetic JSON"}
            if choice is not None:
                body["tool_choice"] = choice
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == (422 if choice == "required" else 200)
        assert received == ["auto", "none"]


def test_exact_limit_persists_and_resets_at_utc_midnight(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    now = datetime(2026, 10, 24, 23, 59, 59, tzinfo=UTC)
    store = UsageStore(path)
    store.reserve_cloud("nano", 600, 1000, 6, now=now)
    store.close()
    store = UsageStore(path)
    store.reserve_cloud("mini", 400, 1000, 6, now=now)
    with pytest.raises(CloudLimitError, match="daily"):
        store.reserve_cloud("nano", 1, 1000, 6, now=now)
    store.reserve_cloud("nano", 1000, 1000, 6, now=now + timedelta(seconds=1))
    store.close()


def test_rolling_rate_limit_is_global_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    now = datetime.now(UTC)
    store = UsageStore(path)
    store.reserve_cloud("nano", 1, 1000, 1, now=now)
    store.close()
    store = UsageStore(path)
    with pytest.raises(CloudLimitError, match="rate limit"):
        store.reserve_cloud("mini", 1, 1000, 1, now=now + timedelta(seconds=59))
    store.reserve_cloud("mini", 1, 1000, 1, now=now + timedelta(seconds=60))
    store.close()


def test_concurrent_process_connections_cannot_overspend(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    UsageStore(path).close()

    def reserve(_: int) -> bool:
        store = UsageStore(path)
        try:
            store.reserve_cloud("nano", 600, 1000, 60)
            return True
        except CloudLimitError:
            return False
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1
    store = UsageStore(path)
    assert store.cloud_budget(1000)["reserved_usd"] == 0.0006
    store.close()


def test_all_rounds_probes_and_failures_reserve_before_http() -> None:
    async def run() -> None:
        requests = 0
        route = cloud_route()

        def handle(_: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(allow_cloud=True), Catalog(
                models=[catalog().models[0], route],
            ), client)
            reservation = math.ceil(4096 * 0.1 + 512 * 0.4)
            for scope in ("chat", "verification"):
                with pytest.raises(httpx.HTTPStatusError):
                    await runtime.complete(route, initial_messages("Synthetic test"), scope)
            assert requests == 2
            budget = runtime.usage.cloud_budget(runtime.cloud_daily_limit)
            assert budget["reserved_usd"] == 2 * reservation / 1_000_000
            assert runtime.usage.summary()["totals"]["failed_calls"] == 2
            runtime.settings.cloud_daily_budget_usd = 0.001
            with pytest.raises(AgentError, match="daily"):
                await runtime.complete(route, initial_messages("Blocked"))
            assert requests == 2
            runtime.usage.close()

    asyncio.run(run())


def test_unpriced_route_and_broken_storage_never_contact_provider() -> None:
    async def run() -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            pytest.fail("Provider must not be contacted")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            route = cloud_route()
            runtime = Runtime(settings(allow_cloud=True), catalog(), client)
            route.input_usd_per_million = route.output_usd_per_million = None
            with pytest.raises(AgentError, match="prices"):
                await runtime.complete(route, initial_messages("Blocked"))
            route = cloud_route()
            runtime.usage.close()
            with pytest.raises(UsageStoreError):
                await runtime.complete(route, initial_messages("Blocked"))

    asyncio.run(run())


def test_paid_readiness_has_hourly_freshness_and_prices() -> None:
    async def run() -> None:
        route = cloud_route()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply())) as client:
            runtime = Runtime(settings(allow_cloud=True), Catalog(
                models=[catalog().models[0], route],
            ), client)
            runtime.states[route.id].verified_at = time.monotonic() - 600
            assert runtime.ready(route)
            assert runtime.model_list()[1]["input_usd_per_million"] == 0.1
            runtime.states[route.id].verified_at = time.monotonic() - 10000
            assert not runtime.ready(route)
            runtime.usage.close()

    asyncio.run(run())


@pytest.mark.parametrize("path", ["/agent", "/ui-api/agent"])
@pytest.mark.parametrize("tool_choice", ["auto", "none"])
def test_rest_and_browser_calls_share_the_same_budget(path: str, tool_choice: str) -> None:
    config = settings(allow_cloud=True, cloud_daily_budget_usd=0.000001)
    with TestClient(create_app(config, background_verify=False)) as client:
        runtime = client.app.state.runtime
        route = cloud_route()
        runtime.catalog.models.append(route)
        runtime.states[route.id] = runtime.states["local-qwen3"]
        runtime.states[route.id].verified_at = time.monotonic()
        headers = (
            {"host": "localhost", "origin": "http://localhost"}
            if path.startswith("/ui-api") else {"Authorization": f"Bearer {TEST_AGENT_KEY}"}
        )
        response = client.post(path, headers=headers, json={
            "model": route.id, "message": "Synthetic input", "tool_choice": tool_choice,
        })
        assert response.status_code == 429
        assert "daily cloud allowance" in response.json()["detail"]
        assert runtime.usage.summary()["totals"]["calls"] == 0
