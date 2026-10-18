"""Offline latency counts with real SQL/guards and mocked provider boundaries."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from ai_services.tests.test_github_app import env, repo  # noqa: F401
from ai_services.tests.test_repository_monitoring_permissions import owned, permissions
from ai_services.tests.test_shared_knowledge import shared, bind  # noqa: F401
from ai_services.decision_guard.orchestration.models import EvaluationJob
from ai_services.decision_guard.orchestration.projections import (
    LIGHT_JOBS, RepositoryReadModel, evaluation_data,
)
from ai_services.decision_guard.orchestration.read_models import (
    CheckPublication, EvaluationProjection, FindingRule, PullRequestState,
)
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.github_app.service import GitHubAppError
from ai_services.graph.knowledge import RepositoryKnowledgeService
from ai_services.graph.repository import GraphRepositoryError
from ai_services.graph.service import RepositoryGraphService
from ai_services.ingestion.persistence.models import (
    ADRModel, GitHubConnectionModel, RepositoryIngestionState, RepositoryModel,
)
from ai_services.ingestion.readiness import GraphReadBarrier, guarded_route
from api_services.app.models.overview import RepositoryOverviewResponse
from api_services.app.routers.repositories import get_repository_overview


@contextmanager
def select_statements(engine):
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def seed_reviews(env, repository_id, count, attention=False):
    """Persist projection fixtures directly; never run ingestion or evaluation."""
    coverage = '{"assessed":1,"uncertain":0,"omitted":0,"errors":0,"scope":"COMPLETE_FOR_RETAINED_SCOPE"}'
    with env.store.session_factory.begin() as session:
        for i in range(count):
            job_id = f'latency_job_{i}'
            session.add(EvaluationJob(
                id=job_id, repository_id=repository_id, pr_number=i + 1,
                revision_key=f'revision_{i}', base_sha='a'*40, head_sha='b'*40,
                event_json='{"title":"Historical title"}', settings_json='{}',
                status='COMPLETED', created_at=100 + i, updated_at=200 + i,
                next_attempt_at=0, result_json='{}',
            ))
            session.add(EvaluationProjection(
                job_id=job_id, evaluation_id=f'eval_{i}', outcome='NO_CONFLICTS_FOUND',
                execution='COMPLETE', coverage_json=coverage, confirmed=0,
                review_required=0, rejected=0, evaluated_at=200 + i,
            ))
            # Missing metadata/publication and stale base/head exercise OUTER JOINs.
            if i % 3 != 0:
                session.add(PullRequestState(
                    repository_id=repository_id, pr_number=i + 1, title=f'PR {i+1}',
                    base_branch='main', head_branch='feature', base_sha='a'*40,
                    head_sha=('c' if i % 3 == 2 else 'b')*40, state='open',
                    github_updated_at=100, observed_at=100, freshness='OBSERVED',
                ))
            if i % 2:
                session.add(CheckPublication(
                    job_id=job_id, head_sha='b'*40, status='PUBLISHED', attempts=1,
                    created_at=100, updated_at=200, next_attempt_at=0,
                ))
            if attention and i == 1:
                session.add(FindingRule(
                    job_id=job_id, finding_id='finding_a', rule_id='decision_a',
                    decision_id='decision_a', requirement_kind='DECISION',
                    adr_ids_json='[]', classification='CONFIRMED',
                ))
    env.app.state.evaluation_orchestrator = SimpleNamespace(
        store=EvaluationStore(env.store.session_factory, clock=lambda: 300))


def wire_graph(env, record):
    """Production-equivalent nested SQL/seal barriers; no external graph server."""
    with env.store.session_factory.begin() as session:
        row = session.get(RepositoryModel, record.id)
        row.status, row.indexed_commit_sha = 'COMPLETED', 'a'*40
        session.add(RepositoryIngestionState(
            repository_id=record.id, epoch=1, state='READY',
            published_run_id='run_1', published_token=1,
        ))
    seal = dict(state='COMPLETE', run_id='run_1', token=1, target='a'*40)
    snapshots = []
    def snapshot(repository_id):
        snapshots.append(repository_id)
        return dict(seal)
    barrier = GraphReadBarrier(env.store.session_factory, SimpleNamespace(snapshot=snapshot))
    graph_repo = SimpleNamespace(
        count_architectural_decisions=lambda scopes: 2,
        get_architectural_knowledge_batch=lambda scopes, ids: [],
    )
    graph = RepositoryGraphService(env.store, graph_repo,
                                   read_authorizer=env.service.authorize_repository_read,
                                   barrier=barrier)
    env.app.state.graph_barrier = barrier
    env.app.state.graph_service = graph
    env.app.state.knowledge_service = RepositoryKnowledgeService(graph)
    return SimpleNamespace(graph=graph, repo=graph_repo, seal=seal, snapshots=snapshots)


def legacy_overview_route(env):
    # The old route wrapper + unchanged handler fallback reproduce the audited
    # four/five independent authorizations without keeping old application code.
    env.app.add_api_route('/legacy/{repo_id}/overview',
        guarded_route(get_repository_overview.__wrapped__),
        response_model=RepositoryOverviewResponse, methods=['GET'])


@pytest.mark.asyncio
@pytest.mark.parametrize('attention,old_passes,snapshots', [(False, 4, 4), (True, 5, 6)])
async def test_overview_discovery_before_after_and_identical_response(env, monkeypatch,
                                                                     attention, old_passes, snapshots):
    record = owned(env)
    graph = wire_graph(env, record)
    seed_reviews(env, record.id, 2, attention)
    legacy_overview_route(env)
    # Warm all installation-token keys outside the measured request.
    await env.service.discovery(env.user.id)
    discovery = AsyncMock(wraps=env.service.discovery)
    monkeypatch.setattr(env.service, 'discovery', discovery)
    env.state['calls'].clear()
    before = env.client.get(f'/legacy/{record.id}/overview', headers=env.headers)
    assert before.status_code == 200, before.text
    old_http = len(env.state['calls'])
    assert discovery.await_count == old_passes
    assert old_http == old_passes * 4
    assert len(graph.snapshots) == snapshots
    discovery.reset_mock()
    graph.snapshots.clear()
    env.state['calls'].clear()
    after = env.client.get(f'/repositories/{record.id}/overview', headers=env.headers)
    assert after.status_code == 200, after.text
    assert after.json() == before.json()
    assert discovery.await_count == 2
    assert len(env.state['calls']) == 8
    assert len(graph.snapshots) == snapshots  # LAT-05 is intentionally unchanged.
    # A later request is fresh, never satisfied from the preceding request.
    env.state['inventory'][10] = []
    assert env.client.get(f'/repositories/{record.id}/overview', headers=env.headers).status_code == 403
    assert env.service._overview_scope.get() is None


@pytest.mark.asyncio
async def test_collaborator_verification_before_after(shared, monkeypatch):
    e = shared.env
    record = await bind(shared)
    graph = wire_graph(e, record)
    seed_reviews(e, record.id, 2, attention=True)
    legacy_overview_route(e)
    await e.service.authorize_repository_read(shared.user.id, record.id)
    e.state['calls'].clear()
    before = e.client.get(f'/legacy/{record.id}/overview', headers=shared.headers)
    assert before.status_code == 200, before.text
    assert len(e.state['calls']) == 20
    graph.snapshots.clear()
    e.state['calls'].clear()
    after = e.client.get(f'/repositories/{record.id}/overview', headers=shared.headers)
    assert after.status_code == 200, after.text
    assert len(e.state['calls']) == 10
    assert after.json() == before.json()
    assert after.json()['permissions'] == dict(can_view_settings=False, can_manage_source_documents=False)
    assert len(graph.snapshots) == 6
    # Membership revoked after graph reading must fail at final verification.
    graph.repo.count_architectural_decisions = lambda _: shared.state.update(member=404) or 2
    assert e.client.get(f'/repositories/{record.id}/overview', headers=shared.headers).status_code == 403


@pytest.mark.parametrize('change,code', [
    ('user_revoke', 401), ('grant', 403), ('private', 403), ('suspend', 403),
    ('owner', 403), ('installation', 403), ('github_id', 403), ('connection', 403),
    ('identity', 403),
    ('epoch', 503), ('seal', 503), ('delete', 503), ('admin', 200),
])
def test_changes_during_overview_fail_closed(env, change, code):
    record = owned(env)
    graph = wire_graph(env, record)
    def mutate(_):
        if change == 'user_revoke':
            env.service.revoke_user('1')
        elif change == 'grant':
            env.state['inventory'][10] = []
        elif change == 'private':
            env.state['inventory'][10][0]['private'] = True
        elif change == 'suspend':
            env.state['installations'][10]['suspended_at'] = '2026-10-18T00:00:00Z'
        elif change == 'admin':
            permissions(env, False)
        elif change == 'seal':
            graph.seal['token'] = 2
        else:
            with env.store.session_factory.begin() as session:
                row = session.get(RepositoryModel, record.id)
                if change == 'owner':
                    row.user_id = 'different-owner'
                elif change == 'installation':
                    row.installation_id = '20'
                elif change == 'github_id':
                    row.github_repository_id = '999'
                elif change == 'connection':
                    row.github_connection_id = 'different-connection'
                elif change == 'identity':
                    session.get(GitHubConnectionModel, env.connection.id).github_user_id = '999'
                elif change == 'epoch':
                    session.get(RepositoryIngestionState, record.id).epoch += 1
                elif change == 'delete':
                    session.add(ADRModel(id='deleted', repository_id=record.id,
                        title='Deleted', status='DELETED', source_name='deleted.md',
                        file_path='unused', content_hash='hash'))
        return 2
    graph.repo.count_architectural_decisions = mutate
    response = env.client.get(f'/repositories/{record.id}/overview', headers=env.headers)
    assert response.status_code == code, response.text
    if change == 'admin':
        assert response.json()['permissions']['can_view_settings'] is False


def test_deletion_during_final_github_check_still_hits_outer_barrier(env, monkeypatch):
    record = owned(env)
    wire_graph(env, record)
    original = env.service.discovery
    calls = 0
    async def discover(user_id):
        nonlocal calls
        calls += 1
        result = await original(user_id)
        if calls == 2:
            with env.store.session_factory.begin() as session:
                session.add(ADRModel(id='late_deleted', repository_id=record.id,
                    title='Deleted', status='DELETED', source_name='deleted.md',
                    file_path='unused', content_hash='hash'))
        return result
    monkeypatch.setattr(env.service, 'discovery', discover)
    assert env.client.get(f'/repositories/{record.id}/overview', headers=env.headers).status_code == 503


@pytest.mark.parametrize('admin', [False, None, 'true', 1])
def test_unknown_or_nonadmin_permissions_do_not_block_owner_read(env, admin):
    record = owned(env)
    wire_graph(env, record)
    permissions(env, admin)
    response = env.client.get(f'/repositories/{record.id}/overview', headers=env.headers)
    assert response.status_code == 200, response.text
    assert response.json()['permissions']['can_view_settings'] is False


def test_not_ready_initial_denial_and_graph_failure_cleanup(env, monkeypatch):
    record = owned(env)
    graph = wire_graph(env, record)
    graph.seal['state'] = 'BUILDING'
    assert env.client.get(f'/repositories/{record.id}/overview', headers=env.headers).status_code == 503
    assert env.service._overview_scope.get() is None
    graph.seal['state'] = 'COMPLETE'
    def unavailable(_):
        raise GraphRepositoryError('offline')
    graph.repo.count_architectural_decisions = unavailable
    response = env.client.get(f'/repositories/{record.id}/overview', headers=env.headers)
    assert response.status_code == 200
    assert response.json()['kpis']['architecture_decisions'] == {
        'value': None, 'availability': {'status': 'error', 'reason': 'graph_query_failed'}}
    assert env.client.get('/repositories/101/overview', headers=env.headers).status_code == 400
    assert env.client.get(f'/repositories/{record.id}/overview').status_code == 401
    env.state['inventory'][10] = []
    assert env.client.get(f'/repositories/{record.id}/overview', headers=env.headers).status_code == 403


def test_live_discovery_can_restore_access_state(env):
    record = owned(env)
    wire_graph(env, record)
    with env.store.session_factory.begin() as session:
        session.get(RepositoryModel, record.id).access_state = 'REMOVED'
    response = env.client.get(f'/repositories/{record.id}/overview', headers=env.headers)
    assert response.status_code == 200, response.text
    assert response.json()['repository']['access_state'] == 'ACTIVE'


@pytest.mark.asyncio
async def test_scope_isolated_and_mutations_and_standalone_reads_stay_fresh(env):
    record = owned(env)
    graph = wire_graph(env, record)
    async with env.service.overview_read_scope(env.user.id, record.id) as scope:
        assert await env.service.authorize_repository_read(env.user.id, record.id) is scope.repository
        permissions(env, False)
        with pytest.raises(GitHubAppError) as denied:
            await env.service.authorize_tracked(env.user.id, record.id, require_admin=True)
        assert denied.value.status_code == 403
        with pytest.raises(GitHubAppError):
            await env.service.authorize_repository_read('other-user', record.id)
        with pytest.raises(GitHubAppError):
            await env.service.authorize_repository_read(env.user.id, 'foreign-repository')
        # Even a child task inheriting a ContextVar cannot reuse an expired scope.
        gate = asyncio.Event()
        async def delayed_read():
            await gate.wait()
            return await env.service.authorize_repository_read(env.user.id, record.id)
        child = asyncio.create_task(delayed_read())
    env.state['inventory'][10] = []
    gate.set()
    with pytest.raises(GitHubAppError):
        await child
    with pytest.raises(GitHubAppError):
        await graph.graph.count_architectural_decisions(record.id, env.user.id)


@pytest.mark.parametrize('count', [0, 1, 10, 100, 101])
def test_list_query_counts_before_after_and_contract(env, monkeypatch, count):
    items = [dict(repo(1000 + i), installation_id='10') for i in range(count)]
    monkeypatch.setattr(env.service, 'discovery', AsyncMock(return_value=([], items)))
    batch = env.store.repository_monitoring_bindings
    # None selects the original capability lookup path, for a measured baseline.
    monkeypatch.setattr(env.store, 'repository_monitoring_bindings', lambda _: None)
    with select_statements(env.engine) as before_queries:
        before = env.client.get('/repositories', headers=env.headers)
    monkeypatch.setattr(env.store, 'repository_monitoring_bindings', batch)
    with select_statements(env.engine) as after_queries:
        after = env.client.get('/repositories', headers=env.headers)
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()
    assert len(before_queries) == 2 + 2 * count  # user + owned bindings + 2R
    assert len(after_queries) == 2 + (2 * count + 199) // 200
    assert not any('JOIN github_connections' in query for query in after_queries)


def test_batch_preserves_foreign_name_alias_and_minimal_columns(env):
    record = owned(env)
    with env.store.session_factory.begin() as session:
        row = session.get(RepositoryModel, record.id)
        row.github_repository_id = '999'  # Name now resolves to a different ID.
        row.user_id = 'other-owner'
    item = dict(repo(), installation_id='10')
    baseline = env.service.monitoring_capabilities(env.user.id, item)
    with select_statements(env.engine) as statements:
        bindings = env.store.repository_monitoring_bindings([item])
        result = env.service.monitoring_capabilities(env.user.id, item, bindings=bindings)
    assert result == baseline
    assert result['reason'] == 'owned_by_another_user'
    assert len(statements) == 1
    assert 'access_token' not in statements[0]


@pytest.mark.parametrize('admin', [True, False, None, 'true', 1])
@pytest.mark.parametrize('status', ['IDLE', 'FAILED', 'COMPLETED'])
def test_owned_batch_policy_matches_live_policy(env, admin, status):
    owned(env, status)
    permissions(env, admin)
    item = dict(env.state['user_repositories'][10][0], installation_id='10')
    baseline = env.service.monitoring_capabilities(env.user.id, item)
    bindings = env.store.repository_monitoring_bindings([item])
    assert env.service.monitoring_capabilities(env.user.id, item, bindings=bindings) == baseline


def legacy_recent(session, repository_id):
    """Audited pre-LAT-06 hydration, retained only as a query-count reference."""
    rows = list(session.execute(select(EvaluationJob, EvaluationProjection)
        .options(*LIGHT_JOBS).join(EvaluationProjection)
        .where(EvaluationJob.repository_id == repository_id)
        .order_by(EvaluationProjection.evaluated_at.desc(), EvaluationJob.id).limit(10)))
    result = []
    for job, projection in rows:
        publication = session.get(CheckPublication, job.id)
        state = session.get(PullRequestState, (repository_id, job.pr_number))
        result.append((evaluation_data(job, projection, publication), state))
    return result


@pytest.mark.parametrize('count', [0, 1, 10, 12])
def test_review_hydration_query_counts_and_history(env, count):
    record = owned(env)
    seed_reviews(env, record.id, count)
    # An overlapping PR number in another repo must never supply the title/state.
    with env.store.session_factory.begin() as session:
        session.add(PullRequestState(repository_id='foreign', pr_number=1, title='Foreign',
            base_branch='main', head_branch='feature', base_sha='a'*40, head_sha='b'*40,
            state='closed', github_updated_at=100, observed_at=100, freshness='OBSERVED'))
    with select_statements(env.engine) as baseline_queries:
        with env.store.session_factory() as session:
            baseline = legacy_recent(session, record.id)
    with select_statements(env.engine) as optimized_queries:
        metrics = RepositoryReadModel(env.app.state.evaluation_orchestrator.store).overview(record.id)
    assert len(baseline_queries) == 1 + 2 * min(count, 10)
    assert len(optimized_queries) == 5
    assert sum('LEFT OUTER JOIN dg_check_publications' in query and
               'LEFT OUTER JOIN dg_pull_requests' in query for query in optimized_queries) == 1
    recent = metrics['recent']
    assert len(recent) == min(count, 10)
    for actual, (expected, state) in zip(recent, baseline, strict=True):
        assert {key: actual[key] for key in expected} == expected
        assert actual['state'] == (state.state if state else 'unknown')
        assert actual['title'] == (state.title if state else 'Historical title')
        assert actual['freshness'] == ('CURRENT_OBSERVED' if state and state.head_sha == 'b'*40
                                       else 'STALE' if state else 'UNKNOWN')
    assert metrics['prs_analyzed'] == count
    assert not any('result_json,' in query or 'context_json,' in query for query in optimized_queries)
