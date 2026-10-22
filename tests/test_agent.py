"""Offline acceptance checks using an HTTP-level gateway and Ollama simulation."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from agent.config import Catalog, Route, Settings, load_catalog
from agent.main import create_app
from agent.runtime import AgentError, Runtime
from gateway.start import build_config

ROOT = Path(__file__).resolve().parents[1]
TEST_AGENT_KEY = "test-agent-key-" + "a" * 32
TEST_GATEWAY_KEY = "test-gateway-key-" + "b" * 32
DIGEST = "sha256:" + "a" * 64


def settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "agent_api_key": TEST_AGENT_KEY,
            "litellm_master_key": TEST_GATEWAY_KEY,
            "catalog_path": ROOT / "models.yaml",
            "local_model_digest": DIGEST,
            "usage_db_path": ":memory:",
            **overrides,
        }
    )


def catalog() -> Catalog:
    return load_catalog(ROOT / "models.yaml")


def reply(
    content: str | None = "42",
    calls: list[dict[str, object]] | None = None,
    finish: str | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                    "finish_reason": finish or ("tool_calls" if calls else "stop"),
                }
            ]
        },
    )


def call(name: str = "calculator", arguments: str = '{"expression":"6 * 7"}') -> dict[str, object]:
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}


def transport(
    *,
    wrong_digest: bool = False,
    inference: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]] | None = None,
) -> httpx.MockTransport:
    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3:4b",
                            "digest": "wrong" if wrong_digest else DIGEST.removeprefix("sha256:"),
                        }
                    ]
                },
            )
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        if request.url.path == "/api/generate":
            body = json.loads(request.content)
            assert body["prompt"] == ""
            assert body["options"] == {
                "num_ctx": 4096,
                "num_gpu": 0,
                "num_predict": 0,
                "use_mmap": True,
            }
            return httpx.Response(200, json={"done": True})
        assert request.url.path == "/v1/chat/completions"
        if inference is not None:
            response = inference(request)
            return response if isinstance(response, httpx.Response) else await response
        assert request.headers["authorization"] == f"Bearer {TEST_GATEWAY_KEY}"
        body = json.loads(request.content)
        assert body["model"] == "local-qwen3"
        assert body["max_tokens"] == 512
        if body["messages"][-1]["role"] == "tool":
            assert json.loads(body["messages"][-1]["content"]) == {"result": 42}
            return reply()
        return reply(None, [call()])

    return httpx.MockTransport(handle)


def test_verified_tool_round_trip_and_readiness() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=transport()) as client:
            runtime = Runtime(settings(), catalog(), client)
            route = runtime.resolve("local-qwen3")
            assert not runtime.ready(route)
            await runtime.verify(route)
            assert runtime.ready(route)
            assert await runtime.run(route.id, "Compute 6 * 7") == "42"
            runtime.states[route.id].verified_at = time.monotonic() - 10000
            assert not runtime.ready(route)

    asyncio.run(run())


@pytest.mark.parametrize("digest", ["", "sha256:" + "b" * 64])
def test_local_digest_requires_exact_approval(digest: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=transport()) as client:
            runtime = Runtime(settings(local_model_digest=digest), catalog(), client)
            route = runtime.resolve("local-qwen3")
            await runtime.verify(route)
            assert not runtime.ready(route)
            assert runtime.states[route.id].verified_at == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: reply("42"),
        lambda _: reply(None, [call("utc_time", "{}")]),
        lambda _: reply(None, [call(arguments='{"expression":"1 + 1"}')]),
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(200, json={"choices": []}),
        lambda _: httpx.Response(200, text="not-json"),
    ],
)
def test_inference_must_succeed_before_readiness(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            config = settings(allow_cloud=True)
            route = Route(
                id="test-cloud",
                label="Test",
                kind="cloud",
                model="openai/test",
                key_env="TEST_API_KEY",
                enabled=True,
            )
            models = Catalog(models=[catalog().models[0], route])
            runtime = Runtime(config, models, client)
            await runtime.verify(route)
            assert not runtime.ready(route)

    asyncio.run(run())


def test_cloud_is_rejected_before_any_http_request() -> None:
    async def run() -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            pytest.fail("Cloud request must never leave the agent")

        models = catalog()
        models.models[1].enabled = True
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            runtime = Runtime(settings(), models, client)
            with pytest.raises(AgentError) as error:
                await runtime.run("openai", "hello")
            assert error.value.status == 403
            with pytest.raises(AgentError) as error:
                await runtime.run("openai/arbitrary-model", "hello")
            assert error.value.status == 404

    asyncio.run(run())


def test_single_concurrent_request_and_no_unverified_inference() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=transport()) as client:
            runtime = Runtime(settings(), catalog(), client)
            async with runtime.lock:
                with pytest.raises(AgentError) as error:
                    await runtime.run("local-qwen3", "hello")
                assert error.value.status == 429
            with pytest.raises(AgentError) as error:
                await runtime.run("local-qwen3", "hello")
            assert error.value.status == 503

    asyncio.run(run())


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (lambda _: reply(None, [call()]), "four-tool execution limit"),
        (lambda _: reply("partial", finish="length"), "truncated"),
        (lambda _: reply(None, [call("shell", "{}")]), "unsafe tool"),
        (lambda _: reply(""), "no answer"),
        (lambda _: httpx.Response(503), "must pass verification"),
    ],
)
def test_bounded_and_failed_inference(
    handler: Callable[[httpx.Request], httpx.Response],
    expected: str,
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=transport(inference=handler)) as client:
            runtime = Runtime(settings(), catalog(), client)
            runtime.states["local-qwen3"].verified_at = time.monotonic()
            with pytest.raises(AgentError) as error:
                await runtime.run("local-qwen3", "hi")
            # Three sequential tool calls hit the round limit before the total call limit.
            if expected == "four-tool execution limit":
                assert "three-round" in error.value.detail
            else:
                assert expected in error.value.detail

    asyncio.run(run())


def test_context_budget_rejects_oversized_input_without_network() -> None:
    async def run() -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            pytest.fail("Oversized prompt must not reach gateway")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            runtime = Runtime(settings(), catalog(), client)
            runtime.states["local-qwen3"].verified_at = time.monotonic()
            with pytest.raises(AgentError) as error:
                await runtime.run("local-qwen3", "x" * 4096)
            assert error.value.status == 413

    asyncio.run(run())


def test_authenticated_api_and_probe_shapes() -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        assert client.get("/healthz").json() == {"status": "alive"}
        assert client.get("/readyz").status_code == 503
        assert client.get("/readyz").json() == {"status": "not_ready"}
        assert client.get("/models").status_code == 401
        assert (
            client.post("/agent", json={"model": "local-qwen3", "message": "hi"}).status_code == 401
        )
        assert client.get("/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": f"Bearer {TEST_AGENT_KEY}"}
        response = client.get("/models", headers=auth)
        assert response.status_code == 200
        assert response.json()["cloud_enabled"] is False
        assert len(response.json()["models"]) == 10
        for model in response.json()["models"]:
            assert not model["ready"]
        assert response.headers["cache-control"] == "no-store"
        assert TEST_GATEWAY_KEY not in response.text
        assert DIGEST not in response.text
        assert client.get("/").status_code == 200
        assert client.get("/.env").status_code == 404
        assert client.get("/docs").status_code == 404
        assert (
            client.post(
                "/agent",
                headers=auth,
                json={
                    "model": "local-qwen3",
                    "message": " ",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/agent",
                headers=auth,
                json={
                    "model": "local-qwen3",
                    "message": "hi",
                    "api_base": "https://untrusted.test",
                },
            ).status_code
            == 422
        )
        assert client.post("/agent", headers=auth, content=b"x" * 16385).status_code == 413


def test_gateway_allowlist_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    models = catalog()
    models.models[1].enabled = True
    config = build_config(settings(), models)
    assert len(config["model_list"]) == 4
    assert all(model["model_name"] != "local-llama32" for model in config["model_list"])
    local = config["model_list"][0]
    assert local["litellm_params"]["num_ctx"] == 4096
    assert local["litellm_params"]["num_gpu"] == 0
    assert local["litellm_params"]["use_mmap"] is True
    assert local["litellm_params"]["think"] is False
    with pytest.raises(ValueError, match="Missing credential"):
        build_config(settings(allow_cloud=True), models)
    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
    config = build_config(settings(allow_cloud=True), models)
    assert len(config["model_list"]) == 5
    assert "test-provider-key" not in json.dumps(config)
    assert config["model_list"][1]["litellm_params"]["api_key"] == "os.environ/OPENAI_API_KEY"


@pytest.mark.parametrize(
    ("model_id", "tag"),
    [("local-phi4-mini", "phi4-mini:3.8b"), ("local-llama32-small", "llama3.2:1b")],
)
@pytest.mark.parametrize("failure", [None, "digest", "tools"])
def test_microsoft_and_meta_candidates_require_verified_tool_inference(
    model_id: str, tag: str, failure: str | None
) -> None:
    async def run() -> None:
        models = catalog()
        route = next(model for model in models.models if model.id == model_id)
        route.enabled = True
        assert route.ollama_model == tag
        assert route.approved_digest and route.approved_digest != DIGEST
        config = build_config(settings(), models)
        params = next(
            model["litellm_params"]
            for model in config["model_list"]
            if model["model_name"] == model_id
        )
        assert params["model"] == "ollama_chat/" + tag
        assert params["num_ctx"] == 4096 and params["num_gpu"] == 0
        assert params["use_mmap"] is True and "think" not in params

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                digest = DIGEST if failure == "digest" else route.approved_digest
                return httpx.Response(200, json={"models": [{"name": tag, "digest": digest}]})
            if request.url.path == "/api/show":
                return httpx.Response(
                    200, json={"capabilities": ["completion"] if failure == "tools" else ["tools"]}
                )
            assert failure is None, "Rejected model must never reach warm-up or inference"
            body = json.loads(request.content)
            if request.url.path == "/api/generate":
                assert body["model"] == tag and body["prompt"] == ""
                return httpx.Response(200, json={"done": True})
            assert request.url.path == "/v1/chat/completions"
            assert body["model"] == model_id
            return reply() if body["messages"][-1]["role"] == "tool" else reply(None, [call()])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            runtime = Runtime(settings(), models, client)
            await runtime.verify(route)
            assert runtime.ready(route) is (failure is None)
            assert runtime.usage.summary("verification")["totals"]["calls"] == (
                2 if failure is None else 0
            )
            runtime.usage.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "local", "model": "ollama_chat/unapproved"},
        {"kind": "local", "model": "ollama_chat/qwen3:4b", "api_base": "http://remote"},
        {"model": "openai/*"},
        {"api_base": "http://remote"},
        {"api_base": "https://user:pass@remote"},
        {"model": "bedrock/test"},
    ],
)
def test_invalid_routes(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Route.model_validate(
            {
                "id": "test",
                "label": "Test",
                "kind": "cloud",
                "model": "openai/test",
                "key_env": "TEST_API_KEY",
                **overrides,
            }
        )


def test_secrets_and_digest_validation() -> None:
    with pytest.raises(ValidationError):
        settings(local_model_digest="unapproved")
    with pytest.raises(ValidationError):
        settings(agent_api_key=SecretStr("short"))
    with pytest.raises(ValidationError):
        settings(agent_api_key=TEST_GATEWAY_KEY)
    assert settings(local_model_digest="a" * 64).local_model_digest == DIGEST


def test_authenticated_api_success_shape() -> None:
    async def run() -> None:
        app = create_app(settings(), background_verify=False)
        async with app.router.lifespan_context(app):
            runtime: Runtime = app.state.runtime
            async with httpx.AsyncClient(transport=transport()) as upstream:
                runtime.client = upstream
                await runtime.verify(runtime.resolve("local-qwen3"))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    assert (await client.get("/readyz")).json() == {"status": "ready"}
                    response = await client.post(
                        "/agent",
                        headers={"Authorization": f"Bearer {TEST_AGENT_KEY}"},
                        json={"model": "local-qwen3", "message": "Compute 6 * 7"},
                    )
                    assert response.status_code == 200
                    assert response.json() == {"model": "local-qwen3", "answer": "42"}

    asyncio.run(run())


def test_actual_overlapping_requests_return_429() -> None:
    async def run() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def handle(_: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return reply()

        async with httpx.AsyncClient(transport=transport(inference=handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            runtime.states["local-qwen3"].verified_at = time.monotonic()
            first = asyncio.create_task(runtime.run("local-qwen3", "hi"))
            await started.wait()
            try:
                with pytest.raises(AgentError) as error:
                    await runtime.run("local-qwen3", "second request")
                assert error.value.status == 429
            finally:
                release.set()
            assert await first == "42"

    asyncio.run(run())


def test_request_deadline_invalidates_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.runtime.REQUEST_SECONDS", 0.01)

    async def run() -> None:
        async def handle(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return reply()

        async with httpx.AsyncClient(transport=transport(inference=handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            route = runtime.resolve("local-qwen3")
            runtime.states[route.id].verified_at = time.monotonic()
            with pytest.raises(AgentError) as error:
                await runtime.run(route.id, "hello")
            assert error.value.status == 502
            assert not runtime.ready(route)
            assert not runtime.lock.locked()

    asyncio.run(run())


def test_fifth_tool_is_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    def tool(name: str, arguments: str) -> str:
        executed.append(name)
        return '{"result":42}'

    monkeypatch.setattr("agent.runtime.execute_tool", tool)

    async def run() -> None:
        rounds = 0

        def handle(_: httpx.Request) -> httpx.Response:
            nonlocal rounds
            rounds += 1
            calls = [dict(call(), id=f"call-{i}") for i in range(4)] if rounds == 1 else [call()]
            return reply(None, calls)

        async with httpx.AsyncClient(transport=transport(inference=handle)) as client:
            runtime = Runtime(settings(), catalog(), client)
            runtime.states["local-qwen3"].verified_at = time.monotonic()
            with pytest.raises(AgentError, match="four-tool execution limit"):
                await runtime.run("local-qwen3", "hello")
            assert len(executed) == 4
            assert rounds == 2

    asyncio.run(run())
