"""Authenticated REST API and automatically connected, loopback-only chat UI."""

import asyncio
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.config import DEVELOPMENT_NOTICE, REVIEW_NOTICE, Settings, load_catalog
from agent.document_api import DocumentService, document_router
from agent.documents import MAX_UPLOAD_BYTES
from agent.runtime import AgentError, Runtime, ToolChoice
from agent.usage import UsageFilter, UsageStore, UsageStoreError

bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class FederalReviewBoundary:
    """Reject processing before request-body buffering, authentication or routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if scope["method"] == "GET" and path == "/healthz":
            response = JSONResponse({"status": "alive", "deployment_profile": "federal-review"})
        elif scope["method"] == "GET" and path in ("/", "/readyz"):
            response = JSONResponse(
                {
                    "status": "review_hold",
                    "deployment_profile": "federal-review",
                    "processing_enabled": False,
                    "authorization": "not_authorized",
                    "detail": REVIEW_NOTICE,
                },
                status_code=503 if path == "/readyz" else 200,
            )
        else:
            logger.warning("Federal review boundary denied a request; body was not read")
            response = JSONResponse(
                {"detail": REVIEW_NOTICE, "processing_enabled": False}, status_code=403
            )
        await response(scope, receive, send)


class BodyLimit:
    def __init__(self, app: ASGIApp, document_uploads: bool = False) -> None:
        self.app = app
        self.document_uploads = document_uploads

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[Message] = []
        size = 0
        limit = (
            MAX_UPLOAD_BYTES
            if self.document_uploads and scope["method"] == "POST"
            and scope["path"] in ("/document-workflows", "/ui-api/document-workflows")
            else 16384
        )
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > limit:
                detail = "Document upload exceeds 64 MiB JSON" if limit > 16384 else (
                    "Request body exceeds 16 KiB"
                )
                await JSONResponse({"detail": detail}, 413)(
                    scope, receive, send
                )
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(chunks):
                message = chunks[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=48)
    message: str = Field(min_length=1, max_length=6000, pattern=r"\S")
    require_local: bool = False
    tool_choice: ToolChoice = "auto"


class AgentResponse(BaseModel):
    model: str
    answer: str


def create_app(settings: Settings | None = None, *, background_verify: bool = True) -> FastAPI:
    config = settings or Settings()
    catalog = load_catalog(config.catalog_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO)
        if config.deployment_profile == "federal-review":
            logger.warning(REVIEW_NOTICE)
            yield
            return
        usage = UsageStore(config.usage_db_path)
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            app.state.runtime = Runtime(config, catalog, client, usage)
            task = (
                asyncio.create_task(app.state.runtime.verification_loop())
                if background_verify
                else None
            )
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                usage.close()

    app = FastAPI(
        title="Intel local agent",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimit, document_uploads=config.temporal_enabled)
    if config.deployment_profile == "federal-review":
        app.add_middleware(FederalReviewBoundary)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), config.agent_api_key.get_secret_value().encode()
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authenticate)]

    async def authenticate_local_ui(request: Request) -> None:
        host = request.headers.get("host", "").lower()
        match = re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", host)
        if match is None or (match[1] is not None and not 1 <= int(match[1]) <= 65535):
            raise HTTPException(403, "Automatic UI access requires a loopback host")
        origin = request.headers.get("origin")
        if (origin is not None and origin != f"{request.url.scheme}://{host}") or (
            request.method == "POST" and origin is None
        ):
            raise HTTPException(403, "Automatic UI access requires a same-origin request")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise HTTPException(403, "Cross-site UI access is not allowed")

    local_ui = [Depends(authenticate_local_ui)]
    documents = DocumentService(config)
    app.state.documents = documents
    app.include_router(document_router(documents, lambda: app.state.runtime, auth, local_ui))

    @app.middleware("http")
    async def headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            if config.deployment_profile == "federal-review"
            else (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        )
        return response

    @app.exception_handler(AgentError)
    async def agent_error(request: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(UsageStoreError)
    async def usage_error(request: Request, exc: UsageStoreError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "Usage storage unavailable. Inference may already have run; "
                "do not retry automatically."
            },
            status_code=503,
        )

    @app.get("/", include_in_schema=False)
    async def ui() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/architecture.svg", include_in_schema=False)
    async def architecture_diagram() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "static" / "architecture.svg",
            media_type="image/svg+xml",
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready(request: Request) -> JSONResponse:
        runtime: Runtime = request.app.state.runtime
        is_ready = any(runtime.ready(route) for route in catalog.models if route.kind == "local")
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    @app.get("/ui-api/models", dependencies=local_ui, include_in_schema=False)
    @app.get("/models", dependencies=auth)
    async def models(request: Request) -> dict[str, object]:
        runtime: Runtime = request.app.state.runtime
        return {
            "cloud_enabled": config.allow_cloud,
            "deployment_notice": DEVELOPMENT_NOTICE,
            "models": runtime.model_list(),
            "cloud_budget": (
                runtime.usage.cloud_budget(runtime.cloud_daily_limit)
                if config.allow_cloud else None
            ),
        }

    @app.get("/ui-api/usage", dependencies=local_ui, include_in_schema=False)
    @app.get("/usage", dependencies=auth)
    async def usage_report(request: Request, scope: UsageFilter = "all") -> dict[str, object]:
        runtime: Runtime = request.app.state.runtime
        return runtime.usage.summary(scope)

    @app.post(
        "/ui-api/agent",
        dependencies=local_ui,
        response_model=AgentResponse,
        include_in_schema=False,
    )
    @app.post("/agent", dependencies=auth, response_model=AgentResponse)
    async def agent(body: AgentRequest, request: Request) -> AgentResponse:
        runtime: Runtime = request.app.state.runtime
        if body.require_local and runtime.resolve(body.model).kind != "local":
            raise HTTPException(403, "This request requires a local model")
        return AgentResponse(
            model=body.model,
            answer=await runtime.run(body.model, body.message, tool_choice=body.tool_choice),
        )

    return app
