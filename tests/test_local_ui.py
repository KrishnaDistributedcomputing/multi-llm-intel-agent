"""Automatic loopback UI access without weakening bearer-authenticated REST routes."""

import asyncio
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent import ROOT, TEST_AGENT_KEY, TEST_GATEWAY_KEY, settings, transport

from agent.main import create_app
from agent.runtime import Runtime


class PanelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.parents: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("role") == "tabpanel":
            panel_id = attributes.get("id")
            assert panel_id is not None
            self.parents[panel_id] = self.stack[-1][1]
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"meta", "link", "img", "input", "br", "hr"}:
            return
        assert self.stack[-1][0] == tag
        self.stack.pop()


def test_documentation_is_a_sibling_panel_and_mermaid_source_matches() -> None:
    parser = PanelParser()
    parser.feed((ROOT / "agent" / "static" / "index.html").read_text(encoding="utf-8"))
    assert set(parser.parents) == {
        "chat-panel", "usage-panel", "documents-panel", "documentation-panel",
        "roi-panel", "eval-panel",
    }
    assert all(parent.get("class") == "workspace" for parent in parser.parents.values())
    assert not parser.stack
    documentation = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    diagrams = re.findall(r"```mermaid\n(.*?)\n```", documentation, re.DOTALL)
    assert len(diagrams) == 3
    assert diagrams[0].strip() == (ROOT / "docs" / "architecture.mmd").read_text(
        encoding="utf-8"
    ).strip()


def test_streamlined_landing_puts_composer_before_collapsed_examples() -> None:
    html = (ROOT / "agent" / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index('id="business-summary"') < html.index('id="chat-form"')
    assert html.index('id="chat-form"') < html.index('id="use-case-examples"')
    assert '<details id="use-case-examples" class="use-case-examples">' in html
    assert '<details class="model-panel">' in html
    assert len(re.findall(r"data-prompt=", html)) == 6
    assert "Public or synthetic data only. Review every answer." in html


def test_bundled_mermaid_svg_is_served_without_keys_or_external_scripts() -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        response = client.get("/architecture.svg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert response.headers["cache-control"] == "no-store"
        svg = ET.fromstring(response.content)
        assert svg.tag == "{http://www.w3.org/2000/svg}svg"
        assert not svg.findall(".//{http://www.w3.org/2000/svg}script")
        assert "Azure AI Foundry" in "".join(svg.itertext())
        links = svg.findall(".//{http://www.w3.org/2000/svg}a")
        assert len(links) == 10
        html = (ROOT / "agent" / "static" / "index.html").read_text(encoding="utf-8")
        for link in links:
            href = link.get("href") or link.get("{http://www.w3.org/1999/xlink}href")
            assert href is not None and re.fullmatch(r"/#component-[a-z]+", href)
            assert f'id="{href.removeprefix("/#")}"' in html
        assert TEST_AGENT_KEY not in response.text
        assert TEST_GATEWAY_KEY not in response.text


@pytest.mark.parametrize(
    "host", ["localhost:8000", "localhost:3001", "127.0.0.1:3001", "[::1]:8000"]
)
def test_local_ui_loads_models_and_usage_without_browser_credentials(host: str) -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        for path in ("/ui-api/models", "/ui-api/usage?scope=chat"):
            response = client.get(path, headers={"host": host, "sec-fetch-site": "same-origin"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert TEST_AGENT_KEY not in response.text
            assert TEST_GATEWAY_KEY not in response.text
        assert len(client.get("/ui-api/models", headers={"host": host}).json()["models"]) == 12
        assert client.get("/models", headers={"host": host}).status_code == 401
        assert client.get("/usage", headers={"host": host}).status_code == 401


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "evil.example",
        "localhost.attacker",
        "127.0.0.2",
        "evil@localhost",
        "localhost:0",
        "localhost:65536",
        "localhost:8000.evil",
    ],
)
def test_auto_ui_rejects_non_loopback_and_invalid_hosts(host: str) -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        assert client.get("/ui-api/models", headers={"host": host}).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "http://evil.example"},
        {"origin": "null"},
        {"origin": "http://localhost:3002"},
        {"origin": "https://localhost:3001"},
        {"origin": "http://127.0.0.1:3001"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
    ],
)
def test_auto_ui_rejects_cross_origin_reads_and_writes(headers: dict[str, str]) -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        guarded = {"host": "localhost:3001", **headers}
        assert client.get("/ui-api/models", headers=guarded).status_code == 403
        assert client.get("/ui-api/usage", headers=guarded).status_code == 403
        assert (
            client.post(
                "/ui-api/agent",
                headers=guarded,
                json={"model": "local-qwen3", "message": "Compute 6 * 7"},
            ).status_code
            == 403
        )
        assert client.app.state.runtime.usage.summary()["totals"]["calls"] == 0


def test_ui_posts_require_origin_and_direct_rest_still_requires_a_token() -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        body = {"model": "local-qwen3", "message": "Compute 6 * 7"}
        headers = {"host": "localhost:3001"}
        assert client.post("/ui-api/agent", headers=headers, json=body).status_code == 403
        headers["origin"] = "http://localhost:3001"
        assert client.post("/agent", headers=headers, json=body).status_code == 401
        headers["authorization"] = f"Bearer {TEST_AGENT_KEY}"
        assert client.get("/models", headers=headers).status_code == 200
        assert client.get("/usage", headers=headers).status_code == 200
        assert client.post("/agent", headers=headers, json=body).status_code == 503
        assert (
            client.post("/ui-api/agent", headers=headers, content=b"x" * 16385).status_code == 413
        )


def test_automatic_ui_chat_uses_the_same_runtime_and_persistent_usage() -> None:
    async def run() -> None:
        app = create_app(settings(), background_verify=False)
        async with app.router.lifespan_context(app):
            runtime: Runtime = app.state.runtime
            async with httpx.AsyncClient(transport=transport()) as upstream:
                runtime.client = upstream
                await runtime.verify(runtime.resolve("local-qwen3"))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:3001"
                ) as client:
                    response = await client.post(
                        "/ui-api/agent",
                        headers={
                            "origin": "http://localhost:3001",
                            "sec-fetch-site": "same-origin",
                        },
                        json={"model": "local-qwen3", "message": "Compute 6 * 7"},
                    )
                    assert response.status_code == 200
                    assert response.json() == {"model": "local-qwen3", "answer": "42"}
                    report = await client.get("/ui-api/usage?scope=chat")
                    assert report.status_code == 200
                    assert report.json()["totals"]["calls"] == 2
                    assert (await client.get("/usage")).status_code == 401

    asyncio.run(run())


def test_browser_shell_has_no_token_entry_or_embedded_credentials() -> None:
    with TestClient(create_app(settings(), background_verify=False)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Connect to your agent" not in page.text
        assert 'type="password"' not in page.text
        assert "Authorization" not in page.text
        assert "Clear conversation" in page.text
        assert "refreshModels();" in page.text
        assert TEST_AGENT_KEY not in page.text
        assert TEST_GATEWAY_KEY not in page.text
