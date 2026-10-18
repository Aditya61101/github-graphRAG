"""Phase A: real durable jobs/transactions; only provider boundaries are mocked."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import Column, MetaData, Table, inspect, select, text
from sqlalchemy.orm import undefer

from ai_services.tests.test_pr_orchestration import env  # noqa: F401
from ai_services.tests.test_frontend_checks import github, metadata  # noqa: F401
from ai_services.tests.test_postgres_schema import pg  # noqa: F401
from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.decision_guard.orchestration.check_lifecycle import LIFECYCLE_COLUMNS
from ai_services.decision_guard.orchestration.checks import (
    CheckPublisher, PublicationChanged, check_payload,
)
from ai_services.decision_guard.orchestration.dispatch import EvaluationOrchestrator
from ai_services.decision_guard.orchestration.models import DispatchIntent, EvaluationJob
from ai_services.decision_guard.orchestration.projections import queue_publication
from ai_services.decision_guard.orchestration.read_models import CheckPublication, PullRequestState
from ai_services.decision_guard.orchestration.worker import EvaluationWorker
from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.migrate_frontend_checks import upgrade, upgrade_check_lifecycle
from ai_services.ingestion.persistence import postgres_schema


@pytest.fixture(autouse=True)
def phase_enabled(monkeypatch):
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'true')
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'true')


def publication(env, job_id):
    with env.sessions() as session:
        return session.scalar(select(CheckPublication).options(undefer('*')).where(
            CheckPublication.job_id == job_id))


@pytest.fixture
def phase(env, github):
    async def request(method, path, token, **kwargs):
        if method == 'GET':
            if '/check-runs/' in path:
                return dict(next(r for r in github.runs if str(r['id']) == path.rsplit('/', 1)[1]))
            return github.pr
        payload = dict(kwargs['json'])
        github.writes.append((method, path, payload))
        if method == 'POST':
            check_id = 100 + len(github.runs)
            remote = dict(payload, id=check_id, app={'id': 42},
                          url=f'https://api.github.com/repos/owner/repo/check-runs/{check_id}')
            github.runs.append(remote)
        else:
            remote = next(r for r in github.runs if str(r['id']) == path.rsplit('/', 1)[1])
            remote.update(payload)
        return dict(remote)
    github.app.request = AsyncMock(side_effect=request)
    publisher = CheckPublisher(env.store, github.app, enabled=True)
    return SimpleNamespace(publisher=publisher, github=github, request=request)


async def enqueue(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    await phase.publisher.publish(job_id)
    return job_id


async def test_lifecycle_same_id_result_contract_and_duplicate_deliveries(env, phase):
    job_id = await enqueue(env, phase)
    row = publication(env, job_id)
    assert (row.desired_state, row.desired_version, row.published_version) == ('queued', 1, 1)
    assert phase.github.writes[0][2]['status'] == 'queued'
    assert 'conclusion' not in phase.github.writes[0][2]
    assert env.store.enqueue('repo_101', env.event, env.settings) == job_id
    env.worker.publisher = phase.publisher
    await env.worker.run_job(job_id)
    assert [w[2]['status'] for w in phase.github.writes] == ['queued', 'in_progress', 'completed']
    assert [w[0] for w in phase.github.writes] == ['POST', 'PATCH', 'PATCH']
    row = publication(env, job_id)
    assert row.check_run_id == '100'
    assert row.desired_version == row.published_version == 3
    result = EvaluationResult.model_validate_json(env.store.get(job_id).result_json)
    expected = check_payload(env.store.get(job_id), result, 'owner/repo')
    expected.pop('head_sha')
    assert phase.github.writes[-1][2] == expected
    spending = env.store.get(job_id).provider_attempts
    await phase.publisher.reconcile()
    assert len(phase.github.writes) == 3
    assert env.store.get(job_id).provider_attempts == spending


async def test_fast_job_coalesces_before_remote_mutation(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    advanced = False
    async def request(method, path, *args, **kwargs):
        nonlocal advanced
        if not advanced and method == 'GET':
            advanced = True
            await env.worker.run_job(job_id)
        return await phase.request(method, path, *args, **kwargs)
    phase.github.app.request.side_effect = request
    await phase.publisher.publish(job_id)
    assert [w[2]['status'] for w in phase.github.writes] == ['completed']
    row = publication(env, job_id)
    assert row.desired_version == row.published_version


async def test_deferred_result_completes_same_check_without_reissuing_unknown_model_call(env, phase):
    job_id = await enqueue(env, phase)
    env.model.fail = True
    env.worker.publisher = phase.publisher
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == 'DEFERRED'
    result = EvaluationResult.model_validate_json(env.store.get(job_id).result_json)
    expected = check_payload(env.store.get(job_id), result, 'owner/repo')
    expected.pop('head_sha')
    assert phase.github.writes[-1][2] == expected
    assert phase.github.runs[0]['status'] == 'completed'
    assert publication(env, job_id).check_run_id == '100'
    assert len(env.model.requests) == 1
    await phase.publisher.reconcile()
    assert len(env.model.requests) == 1


async def test_concurrent_publishers_and_intent_advance_do_not_steal_lease(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(method, *args, **kwargs):
        if method == 'POST':
            entered.set()
            await release.wait()
        return await phase.request(method, *args, **kwargs)
    phase.github.app.request.side_effect = hold
    first = asyncio.create_task(phase.publisher.publish(job_id))
    await entered.wait()
    owner = publication(env, job_id).lease_token
    await asyncio.gather(*(phase.publisher.publish(job_id) for _ in range(5)))
    token = env.store.claim(job_id)
    env.store.fail(job_id, token, 'SETUP_FAILED', retryable=False)
    pending = publication(env, job_id)
    assert pending.status == 'PUBLISHING' and pending.lease_token == owner
    assert pending.desired_version == 3
    release.set()
    await first
    pending = publication(env, job_id)
    assert pending.status == 'RETRY' and pending.published_version == 1
    await phase.publisher.reconcile()
    assert [w[0] for w in phase.github.writes] == ['POST', 'PATCH']
    assert phase.github.writes[-1][2]['conclusion'] == 'action_required'


def test_old_version_fence_cannot_send_or_acknowledge_new_intent(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    owner = phase.publisher.claim(job_id)
    env.store.claim(job_id)
    with pytest.raises(PublicationChanged):
        phase.publisher.save(job_id, owner, expected_version=1)
    assert not phase.github.writes
    env.clock.now += 181
    replacement = phase.publisher.claim(job_id)
    assert replacement and replacement != owner
    with pytest.raises(PublicationChanged):
        phase.publisher.acknowledge(job_id, owner, 1, {'id': 100}, {'status': 'queued'})


async def test_dispatch_failure_never_gates_queued_check_and_expires(env, phase, monkeypatch):
    dispatcher = SimpleNamespace(dispatch=AsyncMock(side_effect=RuntimeError('offline')))
    orchestrator = EvaluationOrchestrator(env.store, env.worker, settings=env.settings,
        dispatcher=dispatcher, publisher=phase.publisher)
    tasks = BackgroundTasks()
    job_id = await orchestrator.accept('repo_101', env.event, tasks)
    assert publication(env, job_id).desired_state == 'queued'
    assert not phase.github.writes  # intent committed before background I/O
    await tasks()
    assert phase.github.runs[0]['status'] == 'queued'
    assert env.store.get(job_id).attempts == 0
    monkeypatch.setenv('GITHUB_CHECK_QUEUE_MAX_AGE_SECONDS', '60')
    env.clock.now += 61
    await orchestrator.reconcile()
    job = env.store.get(job_id)
    assert job.status == 'FAILED' and job.error_code == 'QUEUE_AGE_EXCEEDED'
    assert phase.github.runs[0]['status'] == 'completed'
    assert phase.github.runs[0]['conclusion'] == 'action_required'
    assert not env.model.requests
    assert dispatcher.dispatch.await_count == 1
    assert env.store.claim(job_id) is None  # delayed Cloud Task cannot revive it
    with env.sessions() as session:
        assert session.get(DispatchIntent, job_id) is None


async def test_dispatch_not_blocked_by_unavailable_github(env, phase):
    entered, release, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def unavailable(*args):
        entered.set()
        await release.wait()
        raise RuntimeError('offline')
    async def dispatch(job_id):
        dispatched.set()
    phase.publisher.publish = unavailable
    orchestrator = EvaluationOrchestrator(env.store, env.worker, settings=env.settings,
        dispatcher=SimpleNamespace(dispatch=dispatch), publisher=phase.publisher)
    tasks = BackgroundTasks()
    await orchestrator.accept('repo_101', env.event, tasks)
    background = asyncio.create_task(tasks())
    await entered.wait()
    await asyncio.wait_for(dispatched.wait(), 2)
    release.set()
    await background


async def test_missed_background_work_recovered_without_new_job(env, phase):
    tasks = BackgroundTasks()
    dispatcher = SimpleNamespace(dispatch=AsyncMock())
    orchestrator = EvaluationOrchestrator(env.store, env.worker, settings=env.settings,
        dispatcher=dispatcher, publisher=phase.publisher)
    job_id = await orchestrator.accept('repo_101', env.event, tasks)
    await orchestrator.reconcile()  # never execute lost BackgroundTasks
    assert phase.github.runs[0]['status'] == 'queued'
    dispatcher.dispatch.assert_awaited_once_with(job_id)
    assert len(env.store.list('repo_101')) == 1


async def test_setup_failure_is_claimed_and_finalized_before_model(env, phase):
    job_id = await enqueue(env, phase)
    @asynccontextmanager
    async def broken_runtime():
        assert env.store.get(job_id).status == 'RUNNING'
        assert publication(env, job_id).desired_state == 'in_progress'
        raise ValueError('unavailable runtime configuration')
        yield  # pragma: no cover
    worker = EvaluationWorker(env.store, None, publisher=phase.publisher,
                              runtime_factory=broken_runtime)
    await worker.run_job(job_id)
    assert env.store.get(job_id).status == 'FAILED'
    assert phase.github.runs[0]['status'] == 'completed'
    assert phase.github.runs[0]['conclusion'] == 'action_required'
    assert not env.model.requests


async def test_remote_entrypoint_claims_before_driver_initialization(env, phase, monkeypatch):
    from ai_services.runs import run_pr_evaluation as runner
    job_id = await enqueue(env, phase)
    monkeypatch.setattr(runner, 'SqliteApplicationStore', lambda: env.application_store)
    monkeypatch.setattr(runner, 'EvaluationStore', lambda _: env.store)
    monkeypatch.setattr(runner, 'GitHubAppService', lambda *args: phase.github.app)
    monkeypatch.setattr(runner.GitHubAppSettings, 'from_env', lambda: None)
    monkeypatch.setenv('GITHUB_CHECKS_ENABLED', 'true')
    for name in ['NEO4J_URI', 'NEO4J_USERNAME', 'NEO4J_PASSWORD']:
        monkeypatch.setenv(name, 'offline-test-value')
    def fail_driver(*args, **kwargs):
        assert env.store.get(job_id).status == 'RUNNING'
        assert publication(env, job_id).desired_state == 'in_progress'
        raise ValueError('offline setup error')
    monkeypatch.setattr(runner.GraphDatabase, 'driver', fail_driver)
    assert await runner.run(job_id) == 1
    assert env.store.get(job_id).attempts == 1
    assert phase.github.runs[0]['conclusion'] == 'action_required'


async def test_cancelled_or_timed_out_execution_eventually_terminalizes(env, phase, monkeypatch):
    job_id = await enqueue(env, phase)
    started = asyncio.Event()
    @asynccontextmanager
    async def hold():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover
    worker = EvaluationWorker(env.store, None, publisher=phase.publisher, runtime_factory=hold)
    task = asyncio.create_task(worker.run_job(job_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.store.get(job_id).status == 'RUNNING'
    monkeypatch.setenv('GITHUB_CHECK_QUEUE_MAX_AGE_SECONDS', '60')
    env.clock.now += 61
    env.store.due()
    await phase.publisher.reconcile()
    assert env.store.get(job_id).status == 'FAILED'
    assert phase.github.runs[0]['conclusion'] == 'action_required'


@pytest.mark.parametrize('error', [GitHubAppError(502, 'response lost'), TimeoutError()])
async def test_lost_create_then_closed_pr_reconciles_and_retires_same_check(env, phase, error):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    async def lost(method, *args, **kwargs):
        result = await phase.request(method, *args, **kwargs)
        if method == 'POST':
            raise error
        return result
    phase.github.app.request.side_effect = lost
    await phase.publisher.publish(job_id)
    assert publication(env, job_id).status == 'CREATE_UNKNOWN'
    phase.github.pr.update(metadata(env, state='closed', updated='2026-10-16T02:00:00Z'))
    phase.github.app.request.side_effect = phase.request
    env.clock.now += 10
    await phase.publisher.reconcile()
    assert [w[0] for w in phase.github.writes] == ['POST', 'PATCH']
    assert phase.github.runs[0]['conclusion'] == 'cancelled'
    assert publication(env, job_id).check_run_id == '100'


async def test_ambiguous_absent_check_never_posts_even_after_terminal_failure(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    with env.sessions.begin() as session:
        row = session.get(CheckPublication, job_id)
        row.create_started_at, row.status = 0, 'CREATE_UNKNOWN'
    token = env.store.claim(job_id)
    env.store.fail(job_id, token, 'SETUP', retryable=False)
    await phase.publisher.publish(job_id)
    assert publication(env, job_id).status == 'CREATE_UNKNOWN'
    assert not phase.github.writes


@pytest.mark.parametrize('create_first', [False, True])
async def test_superseded_head_never_creates_obsolete_check(env, phase, create_first):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    if create_first:
        await phase.publisher.publish(job_id)
    newer = env.event.model_copy(update={'head_sha': 'd'*40, 'delivery_id': 'new-head'})
    env.store.enqueue('repo_101', newer, env.settings)
    phase.github.pr.update(metadata(env, head='d'*40, updated='2026-10-16T02:00:00Z'))
    await phase.publisher.publish(job_id)
    if create_first:
        assert [w[0] for w in phase.github.writes] == ['POST', 'PATCH']
        assert phase.github.runs[0]['conclusion'] == 'cancelled'
    else:
        assert not phase.github.writes
        assert publication(env, job_id).status == 'STALE'


async def test_foreign_remote_identity_blocks_patch(env, phase):
    job_id = await enqueue(env, phase)
    phase.github.runs[0]['app']['id'] = 999
    env.store.claim(job_id)
    await phase.publisher.publish(job_id)
    assert publication(env, job_id).status == 'BLOCKED'
    assert len(phase.github.writes) == 1


async def test_compatibility_worker_completion_is_repaired(env, phase, monkeypatch):
    job_id = await enqueue(env, phase)
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false')
    token = env.store.claim(job_id)
    env.store.fail(job_id, token, 'SETUP', retryable=False)
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'true')
    env.store.reconcile_check_lifecycle()
    await phase.publisher.reconcile()
    assert phase.github.runs[0]['conclusion'] == 'action_required'


async def test_compatibility_publisher_stale_mark_cannot_abandon_placeholder(env, phase, monkeypatch):
    job_id = await enqueue(env, phase)
    newer = env.event.model_copy(update={'head_sha': 'd'*40, 'delivery_id': 'later'})
    env.store.enqueue('repo_101', newer, env.settings)
    phase.github.pr.update(metadata(env, head='d'*40, updated='2026-10-16T02:00:00Z'))
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false')
    await phase.publisher.publish(job_id)
    assert publication(env, job_id).status == 'STALE'
    assert phase.github.runs[0]['status'] == 'queued'
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'true')
    await phase.publisher.reconcile()
    assert phase.github.runs[0]['conclusion'] == 'cancelled'


async def test_completed_historical_check_is_preserved_when_pr_closes(env, phase):
    job_id = await enqueue(env, phase)
    env.worker.publisher = phase.publisher
    await env.worker.run_job(job_id)
    prior = dict(phase.github.runs[0])
    writes = len(phase.github.writes)
    phase.github.pr.update(metadata(env, state='closed', updated='2026-10-16T02:00:00Z'))
    phase.publisher.retry(job_id)
    await phase.publisher.reconcile()
    assert phase.github.runs[0] == prior
    assert len(phase.github.writes) == writes
    assert job_id not in phase.publisher.due()


async def test_unknown_freshness_retries_instead_of_cancelling(env, phase):
    job_id = await enqueue(env, phase)
    token = env.store.claim(job_id)
    with env.sessions.begin() as session:
        session.get(PullRequestState, ('repo_101', env.event.pull_request_number)).freshness = 'UNKNOWN'
    original = phase.github.app.request.side_effect
    async def unknown(method, *args, **kwargs):
        value = await original(method, *args, **kwargs)
        if method == 'GET' and '/check-runs/' in args[0]:
            with env.sessions.begin() as session:
                session.get(PullRequestState, ('repo_101', env.event.pull_request_number)).freshness = 'UNKNOWN'
        return value
    # Contradictory same-timestamp metadata remains UNKNOWN after synchronization.
    phase.github.pr.update(metadata(env, state='closed'))
    phase.github.app.request.side_effect = unknown
    await phase.publisher.publish(job_id)
    assert phase.github.runs[0]['status'] == 'queued'
    assert publication(env, job_id).status == 'RETRY'
    assert env.store.get(job_id).lease_token == token


async def test_exhausted_claims_publish_terminal_failure(env, phase):
    job_id = await enqueue(env, phase)
    with env.sessions.begin() as session:
        session.get(EvaluationJob, job_id).max_attempts = 1
    env.store.claim(job_id)
    env.clock.now += 31
    env.store.due()
    await phase.publisher.reconcile()
    assert env.store.get(job_id).error_code == 'ATTEMPTS_EXHAUSTED'
    assert phase.github.runs[0]['conclusion'] == 'action_required'


async def test_cancelled_post_response_keeps_create_marker(env, phase):
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    committed = asyncio.Event()
    async def hold(method, *args, **kwargs):
        response = await phase.request(method, *args, **kwargs)
        if method == 'POST':
            committed.set()
            await asyncio.Event().wait()
        return response
    phase.github.app.request.side_effect = hold
    task = asyncio.create_task(phase.publisher.publish(job_id))
    await committed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert publication(env, job_id).status == 'CREATE_UNKNOWN'
    phase.github.app.request.side_effect = phase.request
    await phase.publisher.reconcile()
    assert len(phase.github.runs) == 1
    assert [w[0] for w in phase.github.writes] == ['POST', 'PATCH']


@pytest.mark.parametrize('age', ['0', '-1', '86401', 'invalid'])
def test_invalid_queue_deadline_rejected_at_configuration(env, monkeypatch, age):
    monkeypatch.setenv('GITHUB_CHECK_QUEUE_MAX_AGE_SECONDS', age)
    with pytest.raises(ValueError):
        EvaluationOrchestrator(env.store, env.worker)


def legacy_publication_table():
    return Table('dg_check_publications', MetaData(), *[
        Column(column.name, column.type, primary_key=column.primary_key, nullable=column.nullable)
        for column in CheckPublication.__table__.columns if column.name not in LIFECYCLE_COLUMNS])


def test_bridge_reads_inserts_before_migration_and_preserves_history(env, monkeypatch):
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false')
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    CheckPublication.__table__.drop(env.engine)
    legacy_publication_table().create(env.engine)
    with env.sessions.begin() as session:
        queue_publication(session, session.get(EvaluationJob, job_id), 1000, 'historical-evaluation')
        row = session.get(CheckPublication, job_id)
        row.check_run_id, row.status, row.conclusion = '1234', 'PUBLISHED', 'neutral'
    with env.sessions() as session:
        assert session.get(CheckPublication, job_id).check_run_id == '1234'
    with env.engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)
    row = publication(env, job_id)
    assert (row.check_run_id, row.status, row.conclusion, row.evaluation_id) == (
        '1234', 'PUBLISHED', 'neutral', 'historical-evaluation')
    assert (row.desired_state, row.desired_version, row.published_version) == ('completed', 0, 0)
    assert LIFECYCLE_COLUMNS <= {c['name'] for c in inspect(env.engine).get_columns('dg_check_publications')}


def test_migration_rolls_back_without_rewriting_historical_rows(env, monkeypatch):
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false')
    CheckPublication.__table__.drop(env.engine)
    legacy_publication_table().create(env.engine)
    with pytest.raises(RuntimeError):
        with env.engine.begin() as conn:
            upgrade(conn)
            raise RuntimeError('rollback test')
    assert not LIFECYCLE_COLUMNS & {c['name'] for c in inspect(env.engine).get_columns('dg_check_publications')}


def test_postgresql_additive_ddl_uses_existing_transaction(monkeypatch):
    inspector = SimpleNamespace(get_columns=lambda name: [
        {'name': c.name} for c in CheckPublication.__table__.columns if c.name not in LIFECYCLE_COLUMNS])
    monkeypatch.setattr('ai_services.ingestion.persistence.migrate_frontend_checks.inspect', lambda conn: inspector)
    conn = MagicMock()
    upgrade_check_lifecycle(conn)
    statements = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert len(statements) == 3
    assert all(sql.startswith('ALTER TABLE dg_check_publications ADD COLUMN') for sql in statements)
    assert all('NOT NULL DEFAULT' in sql for sql in statements)
    assert all('DROP' not in sql and 'UPDATE' not in sql for sql in statements)


def test_validator_accepts_bridge_schema_but_requires_migration_when_enabled(pg, monkeypatch):
    original = pg.inspector.get_columns.side_effect
    pg.inspector.get_columns.side_effect = lambda name, **kw: [
        c for c in original(name, **kw) if name != 'dg_check_publications' or c['name'] not in LIFECYCLE_COLUMNS]
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false')
    postgres_schema.validate_postgresql_schema(pg.engine)
    monkeypatch.setenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'true')
    with pytest.raises(postgres_schema.PostgreSQLSchemaError, match='columns'):
        postgres_schema.validate_postgresql_schema(pg.engine)
    # The explicit upgrade's prevalidation is allowed to see the old shape.
    postgres_schema._validate(pg.conn, allow_missing_components=True)


def test_acceptance_rolls_back_job_and_intent_together(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('transaction failed')
    monkeypatch.setattr('ai_services.decision_guard.orchestration.store.lifecycle_intent', broken)
    with pytest.raises(RuntimeError):
        env.store.enqueue('repo_101', env.event, env.settings)
    assert not env.store.list('repo_101')
    with env.sessions() as session:
        assert not list(session.scalars(select(CheckPublication)))
