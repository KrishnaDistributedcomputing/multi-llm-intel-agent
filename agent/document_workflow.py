"""Deterministic orchestration; document contents never enter workflow history."""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent.config import AGENT_REQUEST_TIMEOUT_SECONDS


@dataclass
class DocumentRun:
    id: str
    total: int
    next_index: int = 0


@workflow.defn
class DocumentWorkflow:
    @workflow.run
    async def run(self, job: DocumentRun) -> int:
        end = min(job.next_index + 100, job.total)
        for index in range(job.next_index, end):
            await workflow.execute_activity(
                "process_document_chunk",
                args=[job.id, index],
                start_to_close_timeout=timedelta(seconds=AGENT_REQUEST_TIMEOUT_SECONDS + 10),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        if end < job.total:
            workflow.continue_as_new(DocumentRun(job.id, job.total, end))
        return job.total
