"""Loopback ingress preserves backend policy and never acts as an open proxy."""

import asyncio

import httpx
import pytest
import yaml
from test_agent import ROOT

from agent.ingress import ROUTES, create_app


def test_ingress_preserves_host_origin_auth_and_backend_status() -> None:
    async def run() -> None:
        calls: list[httpx.Request] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.host == "agent"
            assert request.url.port == 8000
            assert request.headers["host"] == "localhost:18000"
            assert request.headers["origin"] == "http://localhost:18000"
            assert request.headers["authorization"] == "Bearer synthetic-test-only"
            assert "x-forwarded-host" not in request.headers
            return httpx.Response(
                403,
                json={"detail": "Denied by backend"},
                headers={"Content-Security-Policy": "default-src 'self'"},
            )

        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:18000"
                ) as client:
                    for path, methods in ROUTES.items():
                        method = methods[0]
                        response = await client.request(
                            method,
                            path,
                            headers={
                                "origin": "http://localhost:18000",
                                "authorization": "Bearer synthetic-test-only",
                                "x-forwarded-host": "attacker.example",
                            },
                            content=b'{"message":"SYNTHETIC ONLY"}' if method == "POST" else None,
                        )
                        assert response.status_code == 403
                        assert response.json()["detail"] == "Denied by backend"
                        assert response.headers["content-security-policy"] == "default-src 'self'"
                        assert response.headers["cache-control"] == "no-store"
                    response = await client.get(
                        "/usage?scope=chat",
                        headers={
                            "origin": "http://localhost:18000",
                            "authorization": "Bearer synthetic-test-only",
                        },
                    )
                    assert response.status_code == 403
                    assert calls[-1].url.query == b"scope=chat"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("exception", "status"),
    [(httpx.ConnectError("synthetic outage"), 502), (httpx.ReadTimeout("timeout"), 504)],
)
def test_ingress_reports_outages_without_retry(exception: httpx.HTTPError, status: int) -> None:
    async def run() -> None:
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise exception

        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost"
                ) as client:
                    response = await client.post("/agent", json={"message": "synthetic"})
                    assert response.status_code == status
                    assert calls == 1
                    assert response.headers["cache-control"] == "no-store"

    asyncio.run(run())


def test_ingress_denies_unknown_routes_methods_and_large_bodies_without_forwarding() -> None:
    async def run() -> None:
        async def forbidden(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Rejected input must not be forwarded")

        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost"
                ) as client:
                    assert (await client.get("/.env")).status_code == 404
                    assert (await client.get("/http://attacker.example")).status_code == 404
                    assert (await client.post("/models")).status_code == 405
                    assert (await client.get("/agent")).status_code == 405
                    assert (
                        await client.post("/agent", content=b"x" * 16385)
                    ).status_code == 413

    asyncio.run(run())


def test_compose_publishes_only_ingress_without_giving_inference_services_egress() -> None:
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = config["services"]
    for name in ("agent", "gateway", "ollama"):
        assert not services[name].get("ports")
        assert services[name]["networks"] == ["private"]
    ingress = services["ingress"]
    assert ingress["image"] == services["agent"]["image"]
    assert ingress["networks"] == ["private", "ingress"]
    assert not ingress.get("environment")
    assert ingress["ports"] == ["127.0.0.1:${AGENT_PORT:-8000}:8080"]
    assert config["networks"]["private"]["internal"] is True
