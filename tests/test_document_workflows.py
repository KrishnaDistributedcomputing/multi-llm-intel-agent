"""Document boundaries, API policy, activity behavior and real Temporal execution."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from temporalio.api.enums.v1 import PendingActivityState
from temporalio.api.workflow.v1 import PendingActivityInfo
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker
from test_agent import ROOT, TEST_AGENT_KEY, settings, transport

from agent.config import AGENT_REQUEST_TIMEOUT_SECONDS
from agent.document_api import DocumentService
from agent.document_worker import DocumentActivities
from agent.document_workflow import DocumentRun, DocumentWorkflow
from agent.documents import (
    MAX_DOCUMENT_BYTES,
    ChunkResult,
    DocumentRequest,
    DocumentStore,
    chunk_offsets,
)
from agent.main import BodyLimit, create_app
from agent.runtime import AgentError


def document(**changes: object) -> DocumentRequest:
    return DocumentRequest.model_validate({
        "id": str(uuid4()), "name": "synthetic.txt",
        "text": "Public synthetic sample. " * 100, "model": "local-qwen3",
        "instruction": "Summarize this excerpt.", **changes,
    })


@pytest.mark.parametrize("text", [
    "x" * 10001, "😀中文 café\n" * 800,
    "Paragraph one.\r\n\r\n" * 400, 'quote" slash\\ tab\t newline\n' * 500,
])
@pytest.mark.parametrize("overlap", [0, 100, 128])
def test_utf8_chunks_are_bounded_lossless_and_progressing(text: str, overlap: int) -> None:
    data = text.encode()
    covered = 0
    for start, end in chunk_offsets(text, 256, overlap):
        assert start <= covered < end
        assert end - start <= 256
        assert covered - start <= overlap
        data[start:end].decode("utf-8")
        covered = end
    assert covered == len(data)


@pytest.mark.parametrize("changes", [
    {"text": " "}, {"text": "x\0y"}, {"text": "\ud800"}, {"instruction": " "},
    {"instruction": "x" * 601}, {"name": "file.pdf"}, {"max_chunk_bytes": 255},
    {"max_chunk_bytes": 2401}, {"max_chunk_bytes": 256.5}, {"overlap_bytes": -1},
    {"max_chunk_bytes": 256, "overlap_bytes": 129},
    {"text": "x" * (MAX_DOCUMENT_BYTES + 1)},
])
def test_invalid_document_inputs_fail_explicitly(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        document(**changes)


def test_exact_document_limit_and_chunk_count_ceiling() -> None:
    text = "x" * MAX_DOCUMENT_BYTES
    assert chunk_offsets(text, 2400, 0)[-1][1] == MAX_DOCUMENT_BYTES
    with pytest.raises(ValueError, match="10,000 chunks"):
        chunk_offsets(text, 256, 128)


def test_store_is_idempotent_path_safe_and_keeps_ordered_partial_results(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    body = document(name="../../synthetic.txt")
    manifest = store.create(body)
    assert store.create(body) == manifest
    with pytest.raises(ValueError, match="different document"):
        store.create(body.model_copy(update={"text": "Different synthetic text"}))
    with pytest.raises(ValueError):
        store.directory("../outside")
    loaded, prompt = store.chunk(str(body.id), 0)
    assert loaded == manifest
    assert "<document-excerpt>" in prompt
    assert len(json.dumps({"model": body.model, "message": prompt}).encode()) <= 16384
    assert store.result(str(body.id), 0) is None
    store.save_result(str(body.id), ChunkResult(index=1, answer="Synthetic result"))
    report = store.progress(str(body.id), 0, 20)
    assert report["completed"] == 1
    assert report["results"] == [{"index": 1, "answer": "Synthetic result"}]
    assert not list(tmp_path.rglob("*.tmp"))


def test_escaped_chunk_request_limit_is_validated_before_storage(tmp_path: Path) -> None:
    body = document(
        text="\x01" * 2400, instruction="\x01" * 600,
        max_chunk_bytes=2400, overlap_bytes=0,
    )
    with pytest.raises(ValueError, match="request limit"):
        DocumentStore(tmp_path).create(body)
    assert not list(tmp_path.iterdir())


def test_context_budget_is_checked_before_document_storage(tmp_path: Path) -> None:
    body = document(text="x" * 2400, instruction="😀" * 600, max_chunk_bytes=2400)
    with pytest.raises(AgentError, match="context|budget"):
        DocumentStore(tmp_path).create(body)
    assert not list(tmp_path.iterdir())


def test_worker_requests_cannot_switch_to_cloud_after_submission() -> None:
    app = create_app(settings(allow_cloud=True), background_verify=False)
    with TestClient(app) as client:
        runtime = app.state.runtime
        route = runtime.resolve("local-qwen3")
        route.kind = "cloud"
        response = client.post(
            "/agent", json={
                "model": route.id, "message": "Synthetic test", "require_local": True,
            },
            headers={"authorization": f"Bearer {TEST_AGENT_KEY}"},
        )
        assert response.status_code == 403
        assert "local model" in response.json()["detail"]
        assert runtime.usage.summary()["totals"]["calls"] == 0


def test_disabled_and_review_profiles_do_not_start_temporal(tmp_path: Path) -> None:
    app = create_app(settings(document_data_path=tmp_path), background_verify=False)
    with TestClient(app) as client:
        assert client.get("/document-workflows/config").status_code == 401
        assert client.get(
            "/ui-api/document-workflows/config", headers={"host": "localhost"}
        ).json()["enabled"] is False
        response = client.post(
            "/document-workflows",
            headers={"authorization": f"Bearer {TEST_AGENT_KEY}"},
            json=document().model_dump(mode="json"),
        )
        assert response.status_code == 503
        assert not list(tmp_path.iterdir())
    with pytest.raises(ValidationError, match="forbidden"):
        settings(temporal_enabled=True, deployment_profile="federal-review")
    with TestClient(create_app(
        settings(deployment_profile="federal-review"), background_verify=False
    )) as client:
        for method, path in [
            ("POST", "/document-workflows"), ("GET", "/document-workflows/config"),
            ("GET", f"/ui-api/document-workflows/{uuid4()}"),
        ]:
            assert client.request(method, path, content=b"synthetic").status_code == 403


def test_api_upload_progress_cancellation_and_same_origin_guards(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_app(
            settings(temporal_enabled=True, document_data_path=tmp_path), background_verify=False
        )
        temporal = MagicMock(spec=Client)
        temporal.start_workflow = AsyncMock()
        handle = temporal.get_workflow_handle.return_value
        handle.describe = AsyncMock(return_value=MagicMock(status=WorkflowExecutionStatus.RUNNING))
        handle.cancel = AsyncMock()
        service: DocumentService = app.state.documents
        service.client = temporal
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            async with httpx.AsyncClient(transport=transport()) as upstream:
                runtime.client = upstream
                await runtime.verify(runtime.resolve("local-qwen3"))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost"
                ) as client:
                    body = document(text="x" * 20000)
                    headers = {"origin": "http://localhost"}
                    assert (await client.post(
                        "/ui-api/document-workflows", json=body.model_dump(mode="json")
                    )).status_code == 403
                    assert (await client.post(
                        "/ui-api/document-workflows", json=body.model_dump(mode="json"),
                        headers={"origin": "http://evil.example"},
                    )).status_code == 403
                    response = await client.post(
                        "/ui-api/document-workflows", json=body.model_dump(mode="json"),
                        headers=headers,
                    )
                    assert response.status_code == 202
                    assert response.json()["total"] > 1
                    temporal.start_workflow.assert_awaited_once()
                    job = temporal.start_workflow.call_args.args[1]
                    assert isinstance(job, DocumentRun)
                    assert not hasattr(job, "text")
                    store = service.store
                    store.save_result(str(body.id), ChunkResult(index=1, answer="First"))
                    store.save_result(str(body.id), ChunkResult(index=2, answer="Second"))
                    report = await client.get(
                        f"/ui-api/document-workflows/{body.id}?offset=1&limit=1"
                    )
                    assert report.json()["completed"] == 2
                    assert report.json()["results"] == [{"index": 2, "answer": "Second"}]
                    assert report.json()["status"] == "RUNNING"
                    assert (await client.post(
                        f"/ui-api/document-workflows/{body.id}/cancel", headers=headers,
                    )).status_code == 200
                    handle.cancel.assert_awaited_once()
                    assert (await client.get(
                        f"/ui-api/document-workflows/{body.id}?limit=101"
                    )).status_code == 422
                    assert (await client.post(
                        "/ui-api/agent", content=b"x" * 16385, headers=headers,
                    )).status_code == 413
                    temporal.start_workflow.side_effect = WorkflowAlreadyStartedError(
                        str(body.id), "DocumentWorkflow"
                    )
                    assert (await client.post(
                        "/ui-api/document-workflows", json=body.model_dump(mode="json"),
                        headers=headers,
                    )).status_code == 202
                    handle.describe.side_effect = RPCError(
                        "synthetic missing job", RPCStatusCode.NOT_FOUND, b""
                    )
                    assert (await client.get(
                        f"/ui-api/document-workflows/{body.id}"
                    )).status_code == 404

    asyncio.run(run())


def test_document_upload_limit_does_not_expand_other_routes() -> None:
    async def run() -> None:
        async def unreachable(scope: object, receive: object, send: object) -> None:
            raise AssertionError("Oversized upload must not reach the application")

        app = BodyLimit(unreachable, document_uploads=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            assert (await client.post(
                "/ui-api/document-workflows", content=b"x" * (64 * 1024 * 1024 + 1)
            )).status_code == 413
            assert (await client.post("/agent", content=b"x" * 16385)).status_code == 413

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "activity_state", "expected_state", "expected_chunk"),
    [
        (WorkflowExecutionStatus.RUNNING, PendingActivityState.PENDING_ACTIVITY_STATE_STARTED,
         "processing", 7),
        (WorkflowExecutionStatus.RUNNING, PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED,
         "queued", None),
        (
            WorkflowExecutionStatus.RUNNING,
            PendingActivityState.PENDING_ACTIVITY_STATE_CANCEL_REQUESTED, "cancelling", 7,
        ),
        (WorkflowExecutionStatus.COMPLETED, PendingActivityState.PENDING_ACTIVITY_STATE_STARTED,
         "idle", None),
    ],
)
def test_chunk_progress_uses_actual_heartbeat_not_completed_count(
    tmp_path: Path, status: WorkflowExecutionStatus, activity_state: int,
    expected_state: str, expected_chunk: int | None,
) -> None:
    async def run() -> None:
        app = create_app(
            settings(temporal_enabled=True, document_data_path=tmp_path), background_verify=False
        )
        service: DocumentService = app.state.documents
        body = document(text="x" * 20000)
        service.store.create(body)
        temporal = MagicMock(spec=Client)
        temporal.data_converter = DataConverter.default
        pending = PendingActivityInfo(
            activity_type={"name": "process_document_chunk"}, state=activity_state,
            heartbeat_details={"payloads": await DataConverter.default.encode([7])},
        )
        temporal.get_workflow_handle.return_value.describe = AsyncMock(
            return_value=SimpleNamespace(
                status=status, raw_description=DescribeWorkflowExecutionResponse(
                    pending_activities=[pending]
                ),
            )
        )
        service.client = temporal
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as client:
                report = (await client.get(f"/ui-api/document-workflows/{body.id}")).json()
                assert report["completed"] == 0
                assert report["current_chunk"] == expected_chunk
                assert report["chunk_state"] == expected_state

    asyncio.run(run())


@pytest.mark.parametrize("heartbeat", [None, "bad", -1, True, 10000])
def test_missing_or_invalid_chunk_heartbeats_are_not_guessed(
    tmp_path: Path, heartbeat: object,
) -> None:
    async def run() -> None:
        app = create_app(
            settings(temporal_enabled=True, document_data_path=tmp_path), background_verify=False
        )
        service: DocumentService = app.state.documents
        body = document()
        service.store.create(body)
        pending = PendingActivityInfo(
            activity_type={"name": "process_document_chunk"},
            state=PendingActivityState.PENDING_ACTIVITY_STATE_STARTED,
        )
        if heartbeat is not None:
            pending.heartbeat_details.payloads.extend(
                await DataConverter.default.encode([heartbeat])
            )
        temporal = MagicMock(spec=Client)
        temporal.data_converter = DataConverter.default
        temporal.get_workflow_handle.return_value.describe = AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowExecutionStatus.RUNNING,
                raw_description=DescribeWorkflowExecutionResponse(pending_activities=[pending]),
            )
        )
        service.client = temporal
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as client:
                response = await client.get(f"/ui-api/document-workflows/{body.id}")
                if heartbeat is None:
                    assert response.status_code == 200
                    assert response.json()["current_chunk"] is None
                    assert response.json()["chunk_state"] == "processing"
                else:
                    assert response.status_code == 503
                    assert "invalid" in response.json()["detail"]

    asyncio.run(run())


def test_document_activity_deadline_and_preview_proxy_cover_slow_cpu_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        activity = AsyncMock()
        monkeypatch.setattr("agent.document_workflow.workflow.execute_activity", activity)
        assert await DocumentWorkflow().run(DocumentRun(str(uuid4()), 1)) == 1
        assert (
            activity.call_args.kwargs["start_to_close_timeout"].total_seconds()
            > AGENT_REQUEST_TIMEOUT_SECONDS
        )
        assert f"timeout: {AGENT_REQUEST_TIMEOUT_SECONDS * 1000}" in (
            ROOT / "serve.mjs"
        ).read_text()

    asyncio.run(run())


def test_activity_uses_agent_once_caches_results_and_sanitizes_failure(tmp_path: Path) -> None:
    async def run() -> None:
        store = DocumentStore(tmp_path)
        body = document()
        store.create(body)
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.headers["authorization"] == f"Bearer {TEST_AGENT_KEY}"
            assert request.url.path == "/agent"
            assert json.loads(request.content)["require_local"] is True
            return httpx.Response(200, json={"model": body.model, "answer": "Synthetic answer"})

        async with httpx.AsyncClient(
            base_url="http://agent", transport=httpx.MockTransport(upstream)
        ) as client:
            activities = DocumentActivities(store, client, TEST_AGENT_KEY)
            env = ActivityEnvironment()
            heartbeats: list[tuple[object, ...]] = []
            env.on_heartbeat = lambda *details: heartbeats.append(details)
            await env.run(activities.process, str(body.id), 0)
            await env.run(activities.process, str(body.id), 0)
            assert calls == 1
            assert (1,) in heartbeats
            assert store.result(str(body.id), 0).answer == "Synthetic answer"
        async with httpx.AsyncClient(
            base_url="http://agent", transport=httpx.MockTransport(
                lambda request: httpx.Response(503, json={"detail": "synthetic private failure"})
            )
        ) as client:
            activities = DocumentActivities(store, client, TEST_AGENT_KEY)
            with pytest.raises(ApplicationError, match="may already have run") as error:
                await ActivityEnvironment().run(activities.process, str(body.id), 1)
            assert "synthetic private failure" not in str(error.value)
            assert store.result(str(body.id), 1) is None

    asyncio.run(run())


def test_temporal_compose_is_opt_in_private_and_persistent() -> None:
    overlay = yaml.safe_load((ROOT / "compose.temporal.yaml").read_text())
    for name in ("temporal", "document-worker"):
        assert not overlay["services"][name].get("ports")
        assert overlay["services"][name]["networks"] == ["private"]
    assert overlay["services"]["agent"]["environment"]["TEMPORAL_ENABLED"] == "true"
    assert "document-artifacts" in overlay["volumes"]
    assert "temporal-state" in overlay["volumes"]
    assert "temporal" not in yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    assert "temporal" not in yaml.safe_load((ROOT / "compose.review.yaml").read_text())["services"]


@pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"),
    reason="Requires an explicitly selected local server",
)
def test_real_temporal_processes_over_100_chunks_with_continue_as_new(tmp_path: Path) -> None:
    async def run() -> None:
        client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
        store = DocumentStore(tmp_path)
        body = document(text="x" * (256 * 101), max_chunk_bytes=256, overlap_bytes=0)
        manifest = store.create(body)
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"answer": f"Synthetic result {calls}"})

        async with httpx.AsyncClient(
            base_url="http://agent", transport=httpx.MockTransport(upstream)
        ) as agent:
            activities = DocumentActivities(store, agent, TEST_AGENT_KEY)
            async with Worker(
                client, task_queue=f"document-test-{body.id}", workflows=[DocumentWorkflow],
                activities=[activities.process], max_concurrent_activities=1,
            ):
                handle = await client.start_workflow(
                    DocumentWorkflow.run, DocumentRun(str(body.id), len(manifest.offsets)),
                    id=str(body.id), task_queue=f"document-test-{body.id}",
                )
                first_run = handle.first_execution_run_id
                assert await handle.result() == 101
                assert (await handle.describe()).run_id != first_run
                assert calls == 101
                report = store.progress(str(body.id), 100, 20)
                assert report["completed"] == 101
                assert report["results"] == [{"index": 101, "answer": "Synthetic result 101"}]
                history = await handle.fetch_history()
                for event in history.events:
                    payload = event.SerializeToString()
                    assert b"x" * 256 not in payload
                    assert body.instruction.encode() not in payload

    asyncio.run(run())


@pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"),
    reason="Requires an explicitly selected local server",
)
def test_real_temporal_failure_has_no_automatic_inference_retry(tmp_path: Path) -> None:
    async def run() -> None:
        client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
        store = DocumentStore(tmp_path)
        body = document()
        manifest = store.create(body)
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"detail": "synthetic failure"})

        async with httpx.AsyncClient(
            base_url="http://agent", transport=httpx.MockTransport(upstream)
        ) as agent:
            activities = DocumentActivities(store, agent, TEST_AGENT_KEY)
            async with Worker(
                client, task_queue=f"failure-test-{body.id}", workflows=[DocumentWorkflow],
                activities=[activities.process], max_concurrent_activities=1,
            ):
                handle = await client.start_workflow(
                    DocumentWorkflow.run, DocumentRun(str(body.id), len(manifest.offsets)),
                    id=str(body.id), task_queue=f"failure-test-{body.id}",
                )
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                assert calls == 1
                assert store.progress(str(body.id), 0, 20)["completed"] == 0
                assert (await handle.describe()).status == WorkflowExecutionStatus.FAILED

    asyncio.run(run())


@pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"),
    reason="Requires an explicitly selected local server",
)
def test_real_temporal_cancellation_stops_further_chunks(tmp_path: Path) -> None:
    async def run() -> None:
        client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
        store = DocumentStore(tmp_path)
        body = document()
        manifest = store.create(body)
        entered = asyncio.Event()
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.sleep(60)
            return httpx.Response(200, json={"answer": "Synthetic answer"})

        async with httpx.AsyncClient(
            base_url="http://agent", transport=httpx.MockTransport(upstream)
        ) as agent:
            activities = DocumentActivities(store, agent, TEST_AGENT_KEY)
            async with Worker(
                client, task_queue=f"cancel-test-{body.id}", workflows=[DocumentWorkflow],
                activities=[activities.process], max_concurrent_activities=1,
            ):
                handle = await client.start_workflow(
                    DocumentWorkflow.run, DocumentRun(str(body.id), len(manifest.offsets)),
                    id=str(body.id), task_queue=f"cancel-test-{body.id}",
                )
                await asyncio.wait_for(entered.wait(), 20)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                assert calls == 1
                assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED

    asyncio.run(run())


@pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"),
    reason="Requires an explicitly selected local server",
)
def test_real_temporal_reports_each_running_chunk_and_clears_it_on_completion(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        temporal = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
        app = create_app(
            settings(temporal_enabled=True, document_data_path=tmp_path), background_verify=False
        )
        service: DocumentService = app.state.documents
        service.client = temporal
        body = document(text="x" * 512, max_chunk_bytes=256, overlap_bytes=0)
        service.store.create(body)
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return httpx.Response(200, json={"answer": f"Synthetic result {index + 1}"})

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                base_url="http://agent", transport=httpx.MockTransport(upstream)
            ) as agent, httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as api:
                activities = DocumentActivities(service.store, agent, TEST_AGENT_KEY)
                async with Worker(
                    temporal, task_queue=f"progress-test-{body.id}",
                    workflows=[DocumentWorkflow], activities=[activities.process],
                    max_concurrent_activities=1,
                ):
                    handle = await temporal.start_workflow(
                        DocumentWorkflow.run, DocumentRun(str(body.id), 2),
                        id=str(body.id), task_queue=f"progress-test-{body.id}",
                    )
                    try:
                        for index in range(2):
                            await asyncio.wait_for(entered[index].wait(), 20)
                            async with asyncio.timeout(20):
                                while True:
                                    response = await api.get(
                                        f"/ui-api/document-workflows/{body.id}"
                                    )
                                    assert response.status_code == 200
                                    report = response.json()
                                    if report["current_chunk"] == index + 1:
                                        break
                                    await asyncio.sleep(0.2)
                            assert report["chunk_state"] == "processing"
                            assert report["completed"] == index
                            release[index].set()
                        assert await handle.result() == 2
                        report = (await api.get(
                            f"/ui-api/document-workflows/{body.id}"
                        )).json()
                        assert report["current_chunk"] is None
                        assert report["chunk_state"] == "idle"
                        assert report["completed"] == 2
                    finally:
                        for event in release:
                            event.set()

    asyncio.run(run())
