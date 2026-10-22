"""Opt-in authenticated document jobs with reference-only Temporal payloads."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.params import Depends
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from agent.config import Settings
from agent.document_workflow import DocumentRun, DocumentWorkflow
from agent.documents import MAX_DOCUMENT_BYTES, DocumentRequest, DocumentStore
from agent.runtime import Runtime

logger = logging.getLogger(__name__)
RPC_TIMEOUT = timedelta(seconds=10)


class DocumentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = DocumentStore(settings.document_data_path)
        self.client: Client | None = None

    async def temporal(self) -> Client:
        if not self.settings.temporal_enabled:
            raise HTTPException(503, "Temporal document processing is disabled")
        if self.client is None:
            try:
                self.client = await asyncio.wait_for(
                    Client.connect(
                        self.settings.temporal_address, namespace=self.settings.temporal_namespace
                    ),
                    timeout=10,
                )
            except (TimeoutError, RuntimeError, RPCError) as exc:
                logger.warning("Temporal connection unavailable (%s)", type(exc).__name__)
                raise HTTPException(503, "Temporal server is unavailable") from None
        return self.client


def document_router(
    service: DocumentService,
    runtime: Callable[[], Runtime],
    auth: list[Depends],
    local_ui: list[Depends],
) -> APIRouter:
    router = APIRouter()

    @router.get("/ui-api/document-workflows/config", dependencies=local_ui)
    @router.get("/document-workflows/config", dependencies=auth)
    async def configuration() -> dict[str, object]:
        return {
            "enabled": service.settings.temporal_enabled, "max_document_bytes": MAX_DOCUMENT_BYTES,
        }

    @router.post("/ui-api/document-workflows", dependencies=local_ui, status_code=202)
    @router.post("/document-workflows", dependencies=auth, status_code=202)
    async def start(body: DocumentRequest) -> dict[str, object]:
        temporal = await service.temporal()
        current = runtime()
        route = current.resolve(body.model)
        if route.kind != "local" or not current.ready(route):
            raise HTTPException(409, "Select a verified, ready local model for document workflows")
        try:
            manifest = await asyncio.to_thread(service.store.create, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError as exc:
            logger.error("Document storage unavailable (%s)", type(exc).__name__)
            raise HTTPException(503, "Document storage is unavailable") from None
        try:
            await temporal.start_workflow(
                DocumentWorkflow.run,
                DocumentRun(str(body.id), len(manifest.offsets)),
                id=str(body.id), task_queue=service.settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                rpc_timeout=RPC_TIMEOUT,
            )
        except WorkflowAlreadyStartedError:
            return {"id": str(body.id), "total": len(manifest.offsets)}
        except RPCError:
            logger.warning("Temporal workflow submission could not be confirmed")
            raise HTTPException(
                503, "Submission could not be confirmed. Load this workflow ID before retrying; "
                "the job may already be running."
            ) from None
        return {"id": str(body.id), "total": len(manifest.offsets)}

    @router.get("/ui-api/document-workflows/{job_id}", dependencies=local_ui)
    @router.get("/document-workflows/{job_id}", dependencies=auth)
    async def status(
        job_id: UUID, offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        temporal = await service.temporal()
        try:
            description = await temporal.get_workflow_handle(str(job_id)).describe(
                rpc_timeout=RPC_TIMEOUT
            )
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "Document workflow not found") from None
            logger.warning("Temporal workflow status unavailable")
            raise HTTPException(503, "Temporal workflow status is unavailable") from None
        try:
            report = await asyncio.to_thread(service.store.progress, str(job_id), offset, limit)
        except FileNotFoundError:
            logger.error("Document artifacts are missing for a Temporal execution")
            raise HTTPException(503, "Workflow artifacts are missing from local storage") from None
        except OSError as exc:
            logger.error("Document storage unavailable (%s)", type(exc).__name__)
            raise HTTPException(503, "Document storage is unavailable") from None
        report["status"] = description.status.name if description.status else "UNKNOWN"
        return report

    @router.post("/ui-api/document-workflows/{job_id}/cancel", dependencies=local_ui)
    @router.post("/document-workflows/{job_id}/cancel", dependencies=auth)
    async def cancel(job_id: UUID) -> dict[str, str]:
        temporal = await service.temporal()
        try:
            await temporal.get_workflow_handle(str(job_id)).cancel(rpc_timeout=RPC_TIMEOUT)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "Document workflow not found") from None
            logger.warning("Temporal workflow cancellation could not be confirmed")
            raise HTTPException(
                503, "Cancellation could not be confirmed; refresh job status"
            ) from None
        return {"detail": "Cancellation requested. In-flight inference may still finish."}

    return router
