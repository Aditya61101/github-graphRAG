"""Offline transport, original provenance, admission and private worker contracts."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_services.decision_guard.orchestration.gcp import CloudTasksWorkerDispatcher, configured_dispatcher
from ai_services.ingestion.adr.originals import OriginalStorage
from ai_services.tests.test_pr_orchestration import env
from ai_services.tests.test_adr_upload import test_env
from shared.utils.processing import background_processing_enabled


@pytest.mark.parametrize("marker", ["K_SERVICE", "CLOUD_RUN_JOB", "APP_ENV"])
def test_production_defaults_disabled(monkeypatch, marker):
    monkeypatch.delenv("ENABLE_BACKGROUND_PROCESSING", raising=False)
    monkeypatch.setenv(marker, "production")
    assert not background_processing_enabled()
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING", "true")
    assert background_processing_enabled()


def test_remote_rejects_local_database_and_local_dispatch(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "api")
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING", "true")
    monkeypatch.setenv("PR_EVALUATION_DISPATCHER", "local")
    with pytest.raises(ValueError, match="cloud_tasks"):
        configured_dispatcher(None)
    monkeypatch.setenv("PR_EVALUATION_DISPATCHER", "cloud_tasks")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    with pytest.raises(ValueError, match="PostgreSQL"):
        configured_dispatcher(None)


@pytest.mark.parametrize("status", [200, 409, 503])
async def test_tasks_auth_deadline_dedup_and_transport_failure(status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        dispatcher = CloudTasksWorkerDispatcher(http, AsyncMock(return_value="offline"),
            "projects/p/locations/r/queues/q", "https://worker.run.app", "task@p.iam.gserviceaccount.com", clock=lambda: 1000)
        if status == 503:
            with pytest.raises(httpx.HTTPStatusError):
                await dispatcher.dispatch("abc")
            return
        await dispatcher.dispatch("abc")
        await dispatcher.dispatch("abc")
    first, second = [json.loads(r.content)["task"] for r in requests]
    assert first["name"] == second["name"]
    assert first["dispatchDeadline"] == "1800s"
    target = first["httpRequest"]
    assert target["url"] == "https://worker.run.app/evaluate"
    assert target["oidcToken"]["audience"] == "https://worker.run.app"
    assert json.loads(base64.b64decode(target["body"])) == {"job_id": "abc"}


def test_gcs_immutable_write_and_scoped_bounded_read(tmp_path):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b"abcdef"]
    session = Mock()
    session.get.return_value = response
    session.post.return_value.status_code = 200
    storage = OriginalStorage(tmp_path, bucket="originals", session=session)
    location = storage.location("repo", "adr_1", ".md")
    assert location == "gs://originals/repo/adr_1/original.md"
    storage.write(location, b"abc", "text/markdown", "repo", "adr_1")
    assert session.post.call_args.kwargs["params"]["ifGenerationMatch"] == "0"
    assert storage.read(location, "repo", "adr_1", 3) == b"abcd"
    with pytest.raises(ValueError):
        storage.read(location, "other", "adr_1", 3)
    session.post.return_value.status_code = 412
    response.iter_content.return_value = [b"abc"]
    storage.write(location, b"abc", "text/markdown", "repo", "adr_1")
    with pytest.raises(ValueError, match="different content"):
        storage.write(location, b"xyz", "text/markdown", "repo", "adr_1")


async def test_gcs_upload_retry_keeps_original_and_graph_provenance(test_env):
    service = test_env["adr_service"]
    session = Mock()
    session.post.return_value.status_code = 200
    service.originals = OriginalStorage(service.storage_dir, bucket="originals", session=session)
    service.processor = SimpleNamespace(process_adr=AsyncMock(side_effect=RuntimeError("transient")))
    from ai_services.ingestion.adr.service import ADRServiceError
    content = b"# Cloud policy\nEvents follow durable commit."
    with pytest.raises(ADRServiceError):
        await service.process_adr_upload("repo_1001", "usr_alice", "policy.md", content)
    original = test_env["store"].list_adrs("repo_1001")[0]
    service.processor.process_adr.side_effect = None
    record, document = await service.process_adr_upload("repo_1001", "usr_alice", "policy.md", content)
    assert record.id == original.id
    assert record.file_path == original.file_path == f"gs://originals/repo_1001/{record.id}/original.md"
    assert document.file_path == record.file_path
    assert record.status == "COMPLETED"
    assert session.post.call_args.kwargs["data"] == content


async def test_gcs_authority_verifies_hash_and_reads_whole_policy(tmp_path):
    from ai_services.tests.test_decision_guard_evaluation import fixture
    from ai_services.tests.test_evaluation_proof_refinement import store_policy
    from ai_services.decision_guard.evaluation.models import EvaluationSettings
    text = "Events must follow durable commit."
    context, _ = await fixture(policy=text, policy_document=text)
    authority, record, _ = store_policy(tmp_path, text, context)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [text.encode()]
    session = Mock()
    session.get.return_value = response
    authority.originals = OriginalStorage(tmp_path, bucket="originals", session=session)
    record.file_path = authority.originals.location(record.repository_id, record.id, ".md")
    assert authority.receipts(context)[0].status == "APPROVED"
    evidence = authority.policy_evidence(context, adr_ids={record.id}, settings=EvaluationSettings())
    assert evidence.evidence[0].excerpt == text
    response.iter_content.return_value = [b"tampered"]
    assert authority.receipts(context)[0].status == "UNKNOWN"
    session.get.side_effect = OSError("unavailable")
    assert authority.receipts(context)[0].reason == "UPLOAD_SOURCE_UNAVAILABLE"


def test_database_global_admission_and_expired_lease_recovery(env, monkeypatch):
    monkeypatch.setenv("PR_EVALUATION_MAX_CONCURRENCY", "1")
    first = env.store.enqueue("repo_101", env.event, env.settings)
    other = env.event.model_copy(update={"head_sha": "f" * 40, "delivery_id": "other", "pull_request_number": 99})
    second = env.store.enqueue("repo_101", other, env.settings)
    assert env.store.claim(first)
    assert env.store.claim(second) is None
    env.clock.now += 31
    assert env.store.claim(second)


def test_disabled_private_service_never_starts_work(monkeypatch):
    from ai_services.runs.evaluation_service import app
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING", "false")
    with TestClient(app) as client:
        assert client.post("/evaluate", json={"job_id": "a" * 32}).status_code == 503
        assert client.post("/reconcile").status_code == 503


@pytest.mark.parametrize("status,expected", [("COMPLETED", 200), ("QUEUED", 503), ("RUNNING", 503), ("RETRY", 503), ("FAILED", 200)])
def test_private_service_acknowledges_only_terminal_work(monkeypatch, status, expected):
    from ai_services.runs import evaluation_service as service
    from ai_services.runs import run_pr_evaluation
    monkeypatch.setattr(service, "require_enabled", lambda: None)
    monkeypatch.setattr(service, "SqliteApplicationStore", lambda: SimpleNamespace(session_factory=None))
    monkeypatch.setattr(service, "EvaluationStore", lambda _: SimpleNamespace(get=lambda _: SimpleNamespace(status=status)))
    monkeypatch.setattr(run_pr_evaluation, "run", AsyncMock(return_value=1 if status == "FAILED" else 0))
    with TestClient(service.app) as client:
        assert client.post("/evaluate", json={"job_id": "a" * 32}).status_code == expected
