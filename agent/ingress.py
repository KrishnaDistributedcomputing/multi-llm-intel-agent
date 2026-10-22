"""Fixed-destination localhost ingress; inference services remain on a private network."""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from agent.config import AGENT_REQUEST_TIMEOUT_SECONDS
from agent.main import BodyLimit

logger = logging.getLogger(__name__)
ROUTES = {
    "/": ["GET"],
    "/healthz": ["GET"],
    "/readyz": ["GET"],
    "/models": ["GET"],
    "/usage": ["GET"],
    "/agent": ["POST"],
    "/ui-api/models": ["GET"],
    "/ui-api/usage": ["GET"],
    "/ui-api/agent": ["POST"],
    "/document-workflows/config": ["GET"],
    "/document-workflows": ["POST"],
    "/document-workflows/{job_id}": ["GET"],
    "/document-workflows/{job_id}/cancel": ["POST"],
    "/ui-api/document-workflows/config": ["GET"],
    "/ui-api/document-workflows": ["POST"],
    "/ui-api/document-workflows/{job_id}": ["GET"],
    "/ui-api/document-workflows/{job_id}/cancel": ["POST"],
}
REQUEST_HEADERS = ("host", "authorization", "origin", "sec-fetch-site", "content-type")
RESPONSE_HEADERS = (
    "content-type",
    "cache-control",
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "www-authenticate",
    "retry-after",
)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=AGENT_REQUEST_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, document_uploads=os.environ.get("TEMPORAL_ENABLED") == "true")

    @app.middleware("http")
    async def headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        return response

    async def proxy(request: Request) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        try:
            upstream = await client.request(
                request.method,
                f"http://agent:8000{request.url.path}",
                params=request.url.query,
                headers={
                    name: request.headers[name]
                    for name in REQUEST_HEADERS
                    if name in request.headers
                },
                content=await request.body(),
            )
        except httpx.TimeoutException:
            logger.warning("Ingress upstream request timed out")
            return JSONResponse({"detail": "Local agent request timed out"}, status_code=504)
        except httpx.HTTPError as exc:
            logger.warning("Ingress upstream unavailable (%s)", type(exc).__name__)
            return JSONResponse({"detail": "Local agent is unavailable"}, status_code=502)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                name: upstream.headers[name]
                for name in RESPONSE_HEADERS
                if name in upstream.headers
            },
        )

    for path, methods in ROUTES.items():
        app.add_api_route(path, proxy, methods=methods, include_in_schema=False)
    return app
