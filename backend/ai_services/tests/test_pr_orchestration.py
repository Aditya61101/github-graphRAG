"""Offline durable job tests; only GitHub/graph/source/model boundaries are stubbed."""

import asyncio
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from ai_services.decision_guard.evaluation import (
    DecisionIntegrityEvaluator,
    EvaluationResult,
    EvaluationSettings,
)
from ai_services.decision_guard.orchestration.dispatch import (
    CloudRunJobsDispatcher,
    CloudTasksDispatcher,
    EvaluationOrchestrator,
)
from ai_services.decision_guard.orchestration.models import (
    TABLES,
    WebhookDelivery,
)
from ai_services.decision_guard.orchestration.runtime import DatabaseReadiness
from ai_services.decision_guard.orchestration.store import EvaluationStore, LostLease
from ai_services.decision_guard.orchestration.worker import EvaluationWorker
from ai_services.ingestion.persistence.migrate_pr_orchestration import upgrade
from ai_services.ingestion.persistence.models import (
    Base,
    IngestionRunModel,
    RepositoryModel,
    UserModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.pr.models import PullRequestEvent
from ai_services.tests.test_decision_guard_context import BASE, HEAD, REPO, changes
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    fixture,
    tokens,
)
from ai_services.tests.test_pr_ingestion import payload
from api_services.app.routers.evaluations import router as evaluation_router
from api_services.app.routers.webhook import router as webhook_router
from api_services.app.utils.jwt_utils import create_access_token


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FixturePipeline:
    token_counter = staticmethod(tokens)

    def __init__(self, runtime, job):
        self.runtime, self.job = runtime, job

    async def ingest(self):
        self.runtime.ingestions += 1
        return changes()

    async def context(self, change_set):
        self.runtime.contexts += 1
        # This helper executes the actual ArchitectureContextBuilder over stubbed
        # graph and source readers, with actual ADR/RKG fixture evidence.
        context, self.source = await fixture()
        return context

    async def evaluate(self, context, model):
        if not hasattr(self, "source"):
            _, self.source = await fixture()
        return await DecisionIntegrityEvaluator(
            model,
            settings=EvaluationSettings.model_validate_json(self.job.settings_json),
            source=self.source,
            authority=TrustedAuthority(),
            token_counter=tokens,
        ).evaluate(context)


class FixtureRuntime:
    def __init__(self, event, model):
        self.event, self.model = event, model
        self.ingestions = self.contexts = self.opens = 0
        self.current_event = AsyncMock(side_effect=lambda _: self.event)

    @asynccontextmanager
    async def open(self, job, event):
        self.opens += 1
        yield FixturePipeline(self, job)


@pytest.fixture
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    app_store = SqliteApplicationStore(sessions)
    with sessions.begin() as session:
        session.add(UserModel(id="owner", username="owner"))
        session.add(
            RepositoryModel(
                id=REPO,
                github_repository_id="101",
                full_name="owner/repo",
                owner="owner",
                name="repo",
                repository_url="https://github.com/owner/repo",
                user_id="owner",
                installation_id="10",
                tracked_branch="main",
                status="COMPLETED",
                indexed_commit_sha=BASE,
            )
        )
    pr = PullRequestEvent.model_validate(
        {
            k: v
            for k, v in changes().model_dump().items()
            if k in PullRequestEvent.model_fields
        }
    )
    pr = pr.model_copy(update={"delivery_id": "delivery-1"})
    clock = Clock()
    store = EvaluationStore(sessions, clock=clock, lease_seconds=30)
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    runtime = FixtureRuntime(pr, model)
    worker = EvaluationWorker(store, runtime)
    settings = EvaluationSettings(
        max_input_tokens=30000, max_tokens=200000, max_attempts=12
    )
    orchestrator = EvaluationOrchestrator(store, worker, settings=settings)
    app = FastAPI()
    app.include_router(webhook_router, prefix="/webhooks")
    app.include_router(evaluation_router, prefix="/repositories")
    app.state.sqlite_store, app.state.evaluation_orchestrator = app_store, orchestrator
    app.state.github_app = SimpleNamespace(
        store=app_store,
        settings=SimpleNamespace(webhook_secret="test-secret"),
        authorize_tracked=AsyncMock(return_value=app_store.get_repository(REPO)),
        indexing_credential=AsyncMock(),
        installation_token=AsyncMock(return_value=SimpleNamespace(token="offline")),
        request=AsyncMock(return_value={**payload(pr)["pull_request"], "state": "open", "updated_at": "2026-10-16T01:00:00Z"}),
    )
    headers = {"Authorization": "Bearer " + create_access_token({"sub": "owner"})}
    yield SimpleNamespace(
        engine=engine,
        sessions=sessions,
        store=store,
        application_store=app_store,
        event=pr,
        clock=clock,
        runtime=runtime,
        model=model,
        worker=worker,
        service=orchestrator,
        app=app,
        settings=settings,
        headers=headers,
    )
    engine.dispose()


def deliver(client, event, *, signature=True):
    body = json.dumps(payload(event)).encode()
    sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": event.delivery_id or "delivery-1",
            "X-Hub-Signature-256": "sha256=" + (sig if signature else "bad"),
        },
    )


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_verified_webhook_real_stages_confirmed_findings_and_serialization(env, action):
    with TestClient(env.app) as client:
        response = deliver(
            client, env.event.model_copy(update={"source_event": action})
        )
        assert response.status_code == 202
        job_id = response.json()["evaluation_job_id"]
        job = env.store.get(job_id)
        assert job.status == "COMPLETED", job.error_code
        assert job.changes_json and job.context_json
        result = EvaluationResult.model_validate_json(job.result_json)
        assert result.outcome == "CONFLICTS_FOUND"
        assert any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
        assert result.findings[0].citations and result.evidence
        assert env.runtime.ingestions == env.runtime.contexts == 1
        assert env.model.requests and env.model.challenge_requests
        base = f"/repositories/{REPO}/evaluations/{job_id}"
        assert client.get(
            base + "/result", headers=env.headers
        ).json() == result.model_dump(mode="json")
        for removed in ("findings", "coverage", "diagnostics"):
            assert client.get(base + "/" + removed, headers=env.headers).status_code == 404
        diag = env.store.diagnostics(job_id)
        assert diag["is_current_observed_result"] and diag["provider_attempts"] == 2
        assert (
            len(
                client.get(
                    f"/repositories/{REPO}/evaluations", headers=env.headers
                ).json()["runs"]
            )
            == 1
        )
        assert deliver(client, env.event).json()["evaluation_job_id"] == job_id
        assert len(env.model.requests) == 1


def test_signature_and_repository_identity_fail_closed(env):
    with TestClient(env.app) as client:
        assert deliver(client, env.event, signature=False).status_code == 401
        invalid = env.event.model_copy(update={"repository_full_name": "other/repo"})
        assert deliver(client, invalid).status_code == 400
    assert env.store.list(REPO) == []


def test_duplicate_deliveries_and_concurrent_claims(env):
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(
            pool.map(
                lambda _: env.store.enqueue(REPO, env.event, env.settings), range(8)
            )
        )
        tokens = list(pool.map(lambda _: env.store.claim(ids[0]), range(8)))
    assert len(set(ids)) == 1
    assert sum(token is not None for token in tokens) == 1
    assert env.store.get(ids[0]).attempts == 1
    with env.sessions() as session:
        assert len(list(session.scalars(select(WebhookDelivery)))) == 1


@pytest.mark.asyncio
async def test_queued_job_restart_recovery_with_lost_outbox(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    with env.sessions.begin() as session:
        session.execute(text("DELETE FROM pr_evaluation_outbox"))
    restarted = EvaluationOrchestrator(
        EvaluationStore(env.sessions, clock=env.clock), env.worker
    )
    await restarted.reconcile()
    await asyncio.gather(*list(restarted.tasks.values()))
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.runtime.contexts == 1


def test_expired_lease_fences_old_worker_and_all_writes(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    old = env.store.claim(job_id)
    env.clock.now += 31
    assert job_id in env.store.due()
    new = env.store.claim(job_id)
    assert new and new != old
    for method in (
        lambda: env.store.heartbeat(job_id, old),
        lambda: env.store.observe_current(job_id, old),
        lambda: env.store.checkpoint(job_id, old, "INGESTED", changes_json="{}"),
        lambda: env.store.fail(job_id, old, "BAD"),
        lambda: env.store.reserve_packet(job_id, old, "x", "ASSESS", "{}", 1, 0),
    ):
        with pytest.raises(LostLease):
            method()
    assert [a["status"] for a in env.store.diagnostics(job_id)["attempts"]] == [
        "LEASE_EXPIRED",
        "RUNNING",
    ]


@pytest.mark.asyncio
async def test_new_head_supersedes_running_worker_and_late_webhook_reconciles(env):
    old_id = env.store.enqueue(REPO, env.event, env.settings)
    old_token = env.store.claim(old_id)
    newer = env.event.model_copy(
        update={"head_sha": "d" * 40, "delivery_id": "delivery-2"}
    )
    new_id = env.store.enqueue(REPO, newer, env.settings)
    assert env.store.get(old_id).status == "SUPERSEDED"
    with pytest.raises(LostLease):
        env.store.checkpoint(old_id, old_token, "CONTEXT_BUILT", context_json="{}")
    env.runtime.event = newer
    await env.worker.run_job(old_id)
    assert not env.model.requests
    assert env.store.enqueue(REPO, env.event, env.settings) == old_id
    assert new_id in env.store.due()


@pytest.mark.asyncio
async def test_head_advances_without_webhook_old_result_never_current(env):
    old_id = env.store.enqueue(REPO, env.event, env.settings)
    next_event = env.event.model_copy(
        update={"head_sha": "d" * 40, "delivery_id": None}
    )
    env.runtime.current_event.side_effect = [env.event, next_event]
    await env.worker.run_job(old_id)
    job = env.store.get(old_id)
    assert job.status == "SUPERSEDED" and job.result_json
    assert not env.store.diagnostics(old_id)["is_current_observed_result"]
    assert any(
        env.store.get(j).head_sha == next_event.head_sha for j in env.store.due()
    )


def test_webhook_database_failure_rolls_back_before_acknowledgement(env):
    def fail_outbox(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO pr_evaluation_outbox"):
            raise OperationalError(
                statement, parameters, RuntimeError("database unavailable")
            )

    sqlalchemy_event.listen(env.engine, "before_cursor_execute", fail_outbox)
    try:
        with TestClient(env.app) as client:
            assert deliver(client, env.event).status_code == 503
    finally:
        sqlalchemy_event.remove(env.engine, "before_cursor_execute", fail_outbox)
    assert env.store.list(REPO) == []
    with env.sessions() as session:
        assert not list(session.scalars(select(WebhookDelivery)))
    assert not env.model.requests


@pytest.mark.asyncio
async def test_unknown_provider_failure_is_deferred_and_not_reissued(env):
    env.model.fail = True
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    job = env.store.get(job_id)
    assert job.status == "DEFERRED"
    assert len(env.model.requests) == 1
    assert env.store.diagnostics(job_id)["packets"][0]["state"] == "IN_FLIGHT"
    assert json.loads(job.deferred_json)
    env.store.continue_job(job_id)
    await env.worker.run_job(job_id)
    assert len(env.model.requests) == 1
    env.model.fail = False
    env.store.continue_job(job_id, acknowledge_unknown_calls=True)
    await env.worker.run_job(job_id)
    assert len(env.model.requests) == 2
    assert env.store.get(job_id).status == "COMPLETED"


@pytest.mark.asyncio
async def test_partial_coverage_remains_review_required(env):
    env.model.outcomes = {("ADR", "service.py"): "uncertain"}
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    result = EvaluationResult.model_validate_json(env.store.get(job_id).result_json)
    assert result.outcome == "REVIEW_REQUIRED" and result.coverage.uncertain > 0
    assert not env.model.challenge_requests


class Crash(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["INGESTED", "CONTEXT_BUILT", "EVALUATED"])
async def test_crash_recovery_reuses_checkpoints_and_completed_packets(
    env, boundary, monkeypatch
):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    original = env.store.checkpoint
    fired = False

    def checkpoint(*args, **kwargs):
        nonlocal fired
        original(*args, **kwargs)
        if args[2] == boundary and not fired:
            fired = True
            raise Crash()

    monkeypatch.setattr(env.store, "checkpoint", checkpoint)
    with pytest.raises(Crash):
        await env.worker.run_job(job_id)
    env.clock.now += 31
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.runtime.ingestions == env.runtime.contexts == 1
    assert len(env.model.requests) == len(env.model.challenge_requests) == 1


@pytest.mark.asyncio
async def test_completed_assessment_replayed_after_crash_before_challenge(env):
    original = env.model.challenge

    async def crash(request, **kwargs):
        raise asyncio.CancelledError()

    env.model.challenge = crash
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    with pytest.raises(asyncio.CancelledError):
        await env.worker.run_job(job_id)
    env.clock.now += 31
    env.model.challenge = original
    await env.worker.run_job(job_id)
    assert len(env.model.requests) == 1
    assert env.store.get(job_id).status == "DEFERRED"
    assert not env.model.challenge_requests  # Unknown challenge was not reissued.
    env.store.continue_job(job_id, acknowledge_unknown_calls=True)
    await env.worker.run_job(job_id)
    assert len(env.model.requests) == len(env.model.challenge_requests) == 1
    assert env.store.get(job_id).status == "COMPLETED"
    result = EvaluationResult.model_validate_json(env.store.get(job_id).result_json)
    assert any(c.status == "REUSED" for c in result.calls)


@pytest.mark.asyncio
async def test_lifetime_budget_is_preserved_and_remaining_units_are_durable(env):
    settings = env.settings.model_copy(update={"max_attempts": 1})
    job_id = env.store.enqueue(REPO, env.event, settings)
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "DEFERRED"
    assert len(env.model.requests) == 1 and not env.model.challenge_requests
    assert json.loads(env.store.get(job_id).deferred_json)
    env.store.continue_job(job_id, settings=env.settings)
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "COMPLETED"
    assert len(env.model.requests) == len(env.model.challenge_requests) == 1
    assert env.store.diagnostics(job_id)["provider_attempts"] == 2


def test_evaluation_api_auth_and_job_repository_binding(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    base = f"/repositories/{REPO}/evaluations/{job_id}"
    with TestClient(env.app) as client:
        for suffix in ("", "/result"):
            assert client.get(base + suffix).status_code == 401
        assert client.get(base + "/result", headers=env.headers).status_code == 409
        with env.sessions.begin() as session:
            session.add(UserModel(id="other", username="other"))
        other = {"Authorization": "Bearer " + create_access_token({"sub": "other"})}
        assert client.get(base, headers=other).status_code == 403
        assert (
            client.get(
                f"/repositories/{REPO}/evaluations/unknown", headers=env.headers
            ).status_code
            == 404
        )


def test_migration_idempotent_preserves_app_and_postgres_ddl(env):
    with env.engine.begin() as connection:
        upgrade(connection)
        upgrade(connection)
        assert (
            connection.execute(
                text("SELECT version FROM pr_orchestration_schema_version")
            ).scalar()
            == 1
        )
        assert connection.execute(text("SELECT id FROM repositories")).scalar() == REPO
        assert all(
            table.name in inspect(connection).get_table_names() for table in TABLES
        )
    for table in TABLES:
        assert "CREATE TABLE" in str(
            CreateTable(table).compile(dialect=postgresql.dialect())
        )


@pytest.mark.asyncio
async def test_readiness_requires_durable_completed_ingestion_and_rejects_dirty_runs(
    env,
):
    readiness = DatabaseReadiness(env.sessions)
    with pytest.raises(RuntimeError):
        await readiness.snapshot(REPO)
    with env.sessions.begin() as session:
        session.add(
            IngestionRunModel(
                id="run1",
                repository_id=REPO,
                target_commit_sha=BASE,
                status="COMPLETED",
            )
        )
    assert (await readiness.snapshot(REPO)).readiness == "READY"
    with env.sessions.begin() as session:
        session.add(
            IngestionRunModel(
                id="run2", repository_id=REPO, target_commit_sha=HEAD, status="INDEXING"
            )
        )
    assert (await readiness.snapshot(REPO)).readiness == "DIRTY"


@pytest.mark.asyncio
async def test_cloud_dispatch_boundaries_require_no_cloud_credentials():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"name": "operation"})

    async def token():
        return "test-only-token"

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        resource = "projects/test/locations/local/jobs/evaluator"
        await CloudRunJobsDispatcher(client, token, resource).dispatch("abc")
        await CloudTasksDispatcher(
            client,
            token,
            "projects/test/locations/local/queues/evaluations",
            resource,
            "launcher@test.iam.gserviceaccount.com",
        ).dispatch("abc")
    assert (
        json.loads(requests[0].content)["overrides"]["containerOverrides"][0]["env"][0][
            "value"
        ]
        == "abc"
    )
    assert json.loads(requests[1].content)["task"]["httpRequest"]["oauthToken"][
        "serviceAccountEmail"
    ].startswith("launcher@")


@pytest.mark.asyncio
async def test_transient_github_failure_retries_via_reconciliation(env):
    from ai_services.github_app.service import GitHubAppError

    job_id = env.store.enqueue(REPO, env.event, env.settings)
    env.runtime.current_event.side_effect = GitHubAppError(
        503, "test-only upstream failure"
    )
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "RETRY"
    assert not env.model.requests
    env.clock.now += 3
    env.runtime.current_event.side_effect = lambda _: env.event
    await env.service.reconcile()
    await asyncio.gather(*list(env.service.tasks.values()))
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.store.get(job_id).attempts == 2


@pytest.mark.asyncio
async def test_received_provider_error_can_retry_within_lifetime_limits(env):
    from ai_services.decision_guard.evaluation.models import ModelReply

    original = env.model.assess
    first = True

    async def assess(request, **kwargs):
        nonlocal first
        if first:
            first = False
            return ModelReply(
                output_json=None, status="ERROR", input_tokens=10, output_tokens=0
            )
        return await original(request, **kwargs)

    env.model.assess = assess
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.store.diagnostics(job_id)["provider_attempts"] == 3


@pytest.mark.asyncio
async def test_unconfigured_model_denial_does_not_consume_provider_budget(env):
    env.model.dispatch_allowed = False
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "DEFERRED"
    assert env.store.get(job_id).charged_tokens == 0
    assert env.store.get(job_id).provider_attempts == 0
    assert not env.model.requests


@pytest.mark.asyncio
async def test_stale_worker_cannot_repromote_old_pointer(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    token = env.store.claim(job_id)
    newer = env.event.model_copy(update={"head_sha": "d" * 40, "delivery_id": "new"})
    newer_id = env.store.enqueue(REPO, newer, env.settings)
    with pytest.raises(LostLease):
        await env.worker._current(env.store.get(job_id), token, env.event)
    assert env.store.get(newer_id).status == "QUEUED"
    assert env.store.get(job_id).status == "SUPERSEDED"


@pytest.mark.asyncio
async def test_recovery_can_finalize_saved_result_after_last_attempt(env, monkeypatch):
    from ai_services.decision_guard.orchestration.models import EvaluationJob

    job_id = env.store.enqueue(REPO, env.event, env.settings)
    with env.sessions.begin() as session:
        session.get(EvaluationJob, job_id).max_attempts = 1
    original = env.store.finish

    def crash(*args):
        raise Crash()

    monkeypatch.setattr(env.store, "finish", crash)
    with pytest.raises(Crash):
        await env.worker.run_job(job_id)
    assert env.store.get(job_id).stage == "EVALUATED"
    env.clock.now += 31
    assert job_id in env.store.due()
    monkeypatch.setattr(env.store, "finish", original)
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.runtime.opens == 1
    assert len(env.model.requests) == 1


@pytest.mark.asyncio
async def test_dispatch_failure_leaves_durable_recovery_intent(env):
    from fastapi import BackgroundTasks

    class BrokenDispatcher:
        async def dispatch(self, job_id):
            raise RuntimeError("test transport failure")

    service = EvaluationOrchestrator(
        env.store, env.worker, settings=env.settings, dispatcher=BrokenDispatcher()
    )
    job_id = await service.accept(REPO, env.event, BackgroundTasks())
    assert job_id in env.store.due()
    await env.service.reconcile()
    await asyncio.gather(*list(env.service.tasks.values()))
    assert env.store.get(job_id).status == "COMPLETED"


def test_authenticated_continuation_api_requires_monitoring_owner_admin(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    token = env.store.claim(job_id)
    env.store.fail(job_id, token, "TEST_FAILURE", retryable=False)
    with TestClient(env.app) as client:
        path = f"/repositories/{REPO}/evaluations/{job_id}/continue"
        assert client.post(path, json={}).status_code == 401
        response = client.post(
            path,
            json={"settings": env.settings.model_dump(mode="json")},
            headers=env.headers,
        )
        assert response.status_code == 202
        assert env.store.get(job_id).status == "COMPLETED"
        assert env.app.state.github_app.authorize_tracked.call_args.kwargs == {
            "require_admin": True
        }


def test_migration_rollback_is_transactional_on_existing_app(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    orchestration_names = {table.name for table in TABLES}
    Base.metadata.create_all(
        engine,
        tables=[
            table
            for table in Base.metadata.sorted_tables
            if table.name not in orchestration_names
        ],
    )

    def fail_midway(connection, cursor, statement, parameters, context, many):
        if "CREATE TABLE pr_evaluation_heads" in statement:
            raise OperationalError(
                statement, parameters, RuntimeError("migration interrupted")
            )

    sqlalchemy_event.listen(engine, "before_cursor_execute", fail_midway)
    try:
        with pytest.raises(OperationalError), engine.begin() as connection:
            upgrade(connection)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", fail_midway)
    with engine.connect() as connection:
        names = inspect(connection).get_table_names()
        assert "repositories" in names
        assert not orchestration_names.intersection(names)
        assert "pr_orchestration_schema_version" not in names
    with engine.begin() as connection:
        upgrade(connection)
    with engine.connect() as connection:
        assert orchestration_names.issubset(inspect(connection).get_table_names())
    engine.dispose()


@pytest.mark.asyncio
async def test_diagnostics_removed_from_frontend_surface(env):
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    env.runtime.event = env.event.model_copy(update={"head_sha": "d" * 40})
    with TestClient(env.app) as client:
        response = client.get(
            f"/repositories/{REPO}/evaluations/{job_id}/diagnostics",
            headers=env.headers,
        )
        assert response.status_code == 404
        assert env.store.diagnostics(job_id)["is_current_observed_result"] is True


@pytest.mark.asyncio
async def test_evaluation_api_never_returns_other_repositories_source(env):
    with env.sessions.begin() as session:
        session.add(
            RepositoryModel(
                id="repo_202",
                github_repository_id="202",
                full_name="owner/other",
                owner="owner",
                name="other",
                repository_url="https://github.com/owner/other",
                user_id="owner",
            )
        )
    job_id = env.store.enqueue(REPO, env.event, env.settings)
    await env.worker.run_job(job_id)
    with TestClient(env.app) as client:
        for suffix in ("", "/result", "/findings", "/coverage", "/diagnostics"):
            response = client.get(
                f"/repositories/repo_202/evaluations/{job_id}" + suffix,
                headers=env.headers,
            )
            assert response.status_code == 404


@pytest.mark.asyncio
async def test_cloud_acceptance_persists_and_responds_before_remote_launch(env):
    from fastapi import BackgroundTasks

    dispatcher = SimpleNamespace(dispatch=AsyncMock())
    service = EvaluationOrchestrator(
        env.store, env.worker, settings=env.settings, dispatcher=dispatcher
    )
    background = BackgroundTasks()
    job_id = await service.accept(REPO, env.event, background)
    assert env.store.get(job_id).status == "QUEUED"
    dispatcher.dispatch.assert_not_awaited()
    await background()
    dispatcher.dispatch.assert_awaited_once_with(job_id)
