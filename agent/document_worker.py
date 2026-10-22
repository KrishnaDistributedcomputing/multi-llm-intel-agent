"""One-at-a-time local agent activities for Temporal document processing."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from agent.config import Settings
from agent.document_workflow import DocumentWorkflow
from agent.documents import ChunkResult, DocumentStore

logger = logging.getLogger(__name__)


class DocumentActivities:
    def __init__(self, store: DocumentStore, client: httpx.AsyncClient, agent_key: str) -> None:
        self.store = store
        self.client = client
        self.agent_key = agent_key

    @activity.defn(name="process_document_chunk")
    async def process(self, job_id: str, index: int) -> None:
        async def heartbeat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(5)

        pulse = asyncio.create_task(heartbeat())
        try:
            existing = await asyncio.to_thread(self.store.result, job_id, index)
            if existing is not None:
                return
            manifest, prompt = await asyncio.to_thread(self.store.chunk, job_id, index)
            response = await self.client.post(
                "/agent", json={
                    "model": manifest.model, "message": prompt, "require_local": True,
                },
                headers={"Authorization": f"Bearer {self.agent_key}"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
                raise ValueError("Agent returned an invalid result")
            await asyncio.to_thread(
                self.store.save_result, job_id,
                ChunkResult(index=index + 1, answer=payload["answer"]),
            )
        except (httpx.HTTPError, OSError, ValueError, ValidationError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            logger.error(
                "Document chunk processing failed (%s, HTTP status %s)", type(exc).__name__, status
            )
            raise ApplicationError(
                "Chunk processing failed. Inference may already have run; review worker logs "
                "before submitting another job.",
                non_retryable=True,
            ) from None
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse


async def run_worker() -> None:
    settings = Settings()
    if not settings.temporal_enabled or settings.deployment_profile != "development":
        raise ValueError("Document worker requires Temporal-enabled development mode")
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    async with httpx.AsyncClient(
        base_url=settings.document_agent_url, timeout=490, trust_env=False
    ) as agent:
        activities = DocumentActivities(
            DocumentStore(settings.document_data_path), agent,
            settings.agent_api_key.get_secret_value(),
        )
        worker = Worker(
            client, task_queue=settings.temporal_task_queue,
            workflows=[DocumentWorkflow], activities=[activities.process],
            max_concurrent_activities=1,
            graceful_shutdown_timeout=timedelta(seconds=10),
        )
        await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
