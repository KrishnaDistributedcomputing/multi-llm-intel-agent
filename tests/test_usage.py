"""Persistence, honest missing-count handling, and authenticated usage accounting."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_agent import DIGEST, TEST_AGENT_KEY, catalog, reply, settings, transport

from agent.config import Catalog
from agent.main import create_app
from agent.runtime import AgentError, Runtime
from agent.usage import TokenUsage, UsageStore, UsageStoreError


def test_durable_usage_and_scope_filter(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    store = UsageStore(path)
    first = store.start("local-qwen3", "chat")
    store.finish(first, TokenUsage(prompt_tokens=12, completion_tokens=3), "completed")
    second = store.start("local-qwen3-small", "verification")
    store.finish(second, TokenUsage(prompt_tokens=8, completion_tokens=2), "completed")
    store.finish(store.start("local-qwen3", "chat"), None, "failed")
    store.close()

    reopened = UsageStore(path)
    totals = reopened.summary()["totals"]
    assert totals == {
        "calls": 3,
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "failed_calls": 1,
        "pending_calls": 0,
        "unreported_calls": 1,
    }
    assert reopened.summary("chat")["totals"]["calls"] == 2
    assert reopened.summary("verification")["totals"]["total_tokens"] == 10
    assert reopened.summary()["recent"][0]["prompt_tokens"] is None
    reopened.close()


def test_unknown_is_distinct_from_reported_zero(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.sqlite3")
    store.finish(
        store.start("model", "chat"),
        TokenUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        ),
        "completed",
    )
    store.start("model", "chat")
    report = store.summary()
    assert report["totals"]["calls"] == 2
    assert report["totals"]["pending_calls"] == 1
    assert report["totals"]["unreported_calls"] == 1
    assert report["recent"][0]["total_tokens"] is None
    assert report["recent"][1]["total_tokens"] == 0
    assert set(report["recent"][0]) == {
        "id",
        "started_at",
        "model",
        "scope",
        "status",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
    store.close()


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -1},
        {"completion_tokens": 1.5},
        {"prompt_tokens": True},
        {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 3},
        {"total_tokens": "12"},
    ],
)
def test_invalid_usage_not_silently_coerced(usage: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TokenUsage.model_validate(usage)


def test_recent_history_is_bounded(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.sqlite3")
    for _ in range(25):
        store.finish(store.start("model", "chat"), None, "completed")
    report = store.summary()
    assert report["totals"]["calls"] == 25
    assert len(report["recent"]) == 20
    assert report["recent"][0]["id"] == 25
    store.close()


def test_storage_failure_is_explicit(tmp_path: Path) -> None:
    invalid = tmp_path / "not-a-directory"
    invalid.write_text("file", encoding="utf-8")
    with pytest.raises(UsageStoreError):
        UsageStore(invalid / "usage.sqlite3")
    store = UsageStore(tmp_path / "usage.sqlite3")
    with pytest.raises(UsageStoreError):
        store.finish(999, None, "completed")
    store.close()
    with pytest.raises(UsageStoreError):
        store.summary()


def test_every_inference_round_and_probe_is_counted(tmp_path: Path) -> None:
    async def run() -> None:
        original = transport()

        async def handle(request: httpx.Request) -> httpx.Response:
            response = await original.handle_async_request(request)
            if request.url.path.endswith("/chat/completions"):
                body = response.json()
                body["usage"] = {
                    "prompt_tokens": 30,
                    "completion_tokens": 5,
                    "total_tokens": 35,
                }
                return httpx.Response(200, json=body)
            return response

        store = UsageStore(tmp_path / "usage.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), catalog(), client, store)
            route = runtime.resolve("local-qwen3")
            await runtime.verify(route)
            assert await runtime.run(route.id, "Compute 6 * 7") == "42"
            assert store.summary()["totals"]["calls"] == 4
            assert store.summary()["totals"]["total_tokens"] == 140
            assert store.summary("chat")["totals"]["total_tokens"] == 70
            assert store.summary("verification")["totals"]["total_tokens"] == 70
        store.close()

    asyncio.run(run())


def test_truncation_preserves_reported_consumption(tmp_path: Path) -> None:
    async def run() -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            body = reply("partial", finish="length").json()
            body["usage"] = {"prompt_tokens": 4, "completion_tokens": 512, "total_tokens": 516}
            return httpx.Response(200, json=body)

        store = UsageStore(tmp_path / "usage.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), catalog(), client, store)
            with pytest.raises(AgentError, match="truncated"):
                await runtime.complete(runtime.resolve("local-qwen3"), [])
            report = store.summary()
            assert report["totals"]["total_tokens"] == 516
            assert report["totals"]["failed_calls"] == 1
        store.close()

    asyncio.run(run())


def test_usage_endpoint_protected_persistent_and_filtered(tmp_path: Path) -> None:
    config = settings(usage_db_path=tmp_path / "usage.sqlite3")
    auth = {"Authorization": f"Bearer {TEST_AGENT_KEY}"}
    with TestClient(create_app(config, background_verify=False)) as client:
        assert client.get("/usage").status_code == 401
        assert client.get("/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
        store = client.app.state.runtime.usage
        store.finish(
            store.start("local-qwen3", "chat"),
            TokenUsage(
                prompt_tokens=7,
                completion_tokens=2,
            ),
            "completed",
        )
        assert client.get("/usage?scope=invalid", headers=auth).status_code == 422
    with TestClient(create_app(config, background_verify=False)) as client:
        response = client.get("/usage?scope=chat", headers=auth)
        assert response.status_code == 200
        assert response.json()["totals"]["total_tokens"] == 9
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/usage?scope=verification", headers=auth).json()["totals"]["calls"] == 0
        assert "Not authorized for federal nonpublic information" in client.get("/").text


def test_multiple_local_approvals_are_independent() -> None:
    async def run() -> None:
        models = catalog()
        small = next(route for route in models.models if route.id == "local-qwen3-small")
        assert small.enabled and small.approved_digest
        small.approved_digest = ""
        async with httpx.AsyncClient(transport=transport()) as client:
            runtime = Runtime(settings(local_model_digest=DIGEST), models, client)
            await runtime.verify(small)
            assert not runtime.ready(small)
            assert "digest approval" in runtime.states[small.id].reason
            route = runtime.resolve("local-qwen3")
            await runtime.verify(route)
            assert runtime.ready(route)

    asyncio.run(run())


def test_catalog_accepts_alternate_local_default() -> None:
    models = catalog()
    models.models[0].enabled = False
    Catalog.model_validate(models.model_dump())
    for route in models.models:
        if route.kind == "local":
            route.enabled = False
    with pytest.raises(ValidationError):
        Catalog.model_validate(models.model_dump())


def test_usage_storage_failure_returns_503() -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        client.app.state.runtime.usage.close()
        response = client.get("/usage", headers={"Authorization": f"Bearer {TEST_AGENT_KEY}"})
        assert response.status_code == 503
        assert "do not retry automatically" in response.json()["detail"]


def test_incomplete_warmup_cannot_mark_local_model_ready() -> None:
    async def run() -> None:
        original = transport()

        async def handle(request: httpx.Request) -> httpx.Response:
            assert request.url.path != "/v1/chat/completions"
            if request.url.path == "/api/generate":
                return httpx.Response(200, json={"done": False})
            return await original.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            route = runtime.resolve("local-qwen3")
            await runtime.verify(route)
            assert not runtime.ready(route)
            assert runtime.usage.summary()["totals"]["calls"] == 0

    asyncio.run(run())


def test_failed_llama_candidate_is_disabled_without_outbound_inference() -> None:
    async def run() -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            pytest.fail("Disabled model must never leave the agent")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            runtime = Runtime(settings(), catalog(), client)
            with pytest.raises(AgentError) as error:
                await runtime.run("local-llama32", "Compute 6 * 7")
            assert error.value.status == 403
            assert runtime.usage.summary()["totals"]["calls"] == 0

    asyncio.run(run())


def test_preparation_precedes_user_inference_without_counting_as_tokens() -> None:
    async def run() -> None:
        paths: list[str] = []
        original = transport()

        async def handle(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return await original.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            route = runtime.resolve("local-qwen3")
            runtime.states[route.id].verified_at = time.monotonic()
            assert await runtime.run(route.id, "Compute 6 * 7") == "42"
            assert paths == [
                "/api/tags",
                "/api/show",
                "/api/generate",
                "/v1/chat/completions",
                "/v1/chat/completions",
            ]
            assert runtime.usage.summary()["totals"]["calls"] == 2

    asyncio.run(run())


def test_preparation_deadline_invalidates_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.runtime.MODEL_LOAD_SECONDS", 0.01)

    async def run() -> None:
        original = transport()

        async def handle(request: httpx.Request) -> httpx.Response:
            assert request.url.path != "/v1/chat/completions"
            if request.url.path == "/api/generate":
                await asyncio.sleep(1)
            return await original.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            route = runtime.resolve("local-qwen3")
            runtime.states[route.id].verified_at = time.monotonic()
            with pytest.raises(AgentError, match="must pass verification"):
                await runtime.run(route.id, "Compute 6 * 7")
            assert not runtime.ready(route)
            assert not runtime.lock.locked()
            assert runtime.usage.summary()["totals"]["calls"] == 0

    asyncio.run(run())


def test_readiness_expires_after_a_bounded_serial_verification_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.runtime.time", SimpleNamespace(monotonic=lambda: 10000))

    async def run() -> None:
        models = catalog()
        for route in models.models:
            if route.kind == "local":
                route.enabled = route.id in {"local-qwen3-small", "local-llama32"}
        async with httpx.AsyncClient(transport=transport()) as client:
            runtime = Runtime(settings(verify_interval_seconds=300), models, client)
            route = runtime.resolve("local-qwen3-small")
            runtime.states[route.id].verified_at = 10000 - 1289
            assert runtime.ready(route)
            runtime.states[route.id].verified_at = 10000 - 1291
            assert not runtime.ready(route)
            runtime.usage.close()

    asyncio.run(run())
