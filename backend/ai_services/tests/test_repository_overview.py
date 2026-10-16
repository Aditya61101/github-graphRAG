from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_services.github_app.service import GitHubAppError
from ai_services.graph.repository import GraphRepositoryError, Neo4jGraphRepository
from ai_services.graph.service import RepositoryAccessDeniedError, RepositoryNotFoundError
from ai_services.ingestion.persistence.models import RepositoryModel
from ai_services.tests.test_repository_graph import graph_env


@pytest.fixture
def overview_env(graph_env):
    env = graph_env
    env['decisions'] = [
        {'node': 1, 'repository': 'repo_a'},
        {'node': 2, 'repository': '1001'},
        {'node': 3, 'repository': 'repo_b'},
    ]
    env['calls'] = []

    def execute(query, database_, **params):
        assert database_ == 'neo4j'
        assert 'count(DISTINCT d)' in query
        assert 'd.repository IN $repo_ids' in query
        env['calls'].append(params)
        if env['driver'].should_fail:
            raise RuntimeError('offline')
        nodes = {d['node'] for d in env['decisions'] if d['repository'] in params['repo_ids']}
        return [{'decision_count': len(nodes)}], None, None

    env['driver'].execute_query = execute

    def authorize(user_id, identifier, require_admin=False):
        if require_admin and env.get('admin_error'):
            raise GitHubAppError(env['admin_error'], 'permission unavailable')
        if env.get('read_error'):
            raise GitHubAppError(env['read_error'], 'repository access unavailable')
        return env['store'].get_repository(identifier)

    env['client'].app.state.github_app = SimpleNamespace(authorize_tracked=AsyncMock(side_effect=authorize))
    env['headers'] = {'Authorization': f"Bearer {env['alice_token']}"}
    return env


def fetch(env, identifier='repo_a'):
    return env['client'].get(f'/repositories/{identifier}/overview', headers=env['headers'])


def test_authorized_decision_count_and_legacy_scope(overview_env):
    env = overview_env
    env['decisions'].append(env['decisions'][0])
    response = fetch(env)
    assert response.status_code == 200
    result = response.json()
    assert result['kpis']['architecture_decisions'] == {
        'value': 2, 'availability': {'status': 'available', 'reason': None},
    }
    assert env['calls'] == [{'repo_ids': ['repo_a', '1001']}]
    assert result['decision_count_basis'] == 'distinct_persisted_nodes'
    assert result['permissions']['can_view_settings'] is True
    assert result['repository']['id'] == 'repo_a'


def test_zero_decisions_is_available(overview_env):
    overview_env['decisions'] = []
    metric = fetch(overview_env).json()['kpis']['architecture_decisions']
    assert metric['value'] == 0
    assert metric['availability']['status'] == 'available'


@pytest.mark.parametrize('missing_service', [False, True])
def test_graph_failure_is_never_zero(overview_env, missing_service):
    overview_env['driver'].should_fail = True
    if missing_service:
        overview_env['client'].app.state.graph_service = None
    response = fetch(overview_env)
    assert response.status_code == 200
    metric = response.json()['kpis']['architecture_decisions']
    assert metric['value'] is None
    assert metric['availability'] == {'status': 'error', 'reason': 'graph_query_failed'}


@pytest.mark.parametrize('identifier,code', [('repo_b', 403), ('repo_unknown', 404), ('invalid', 404), ('1001', 400)])
def test_unauthorized_unknown_or_noncanonical_scope(overview_env, identifier, code):
    assert fetch(overview_env, identifier).status_code == code
    assert overview_env['calls'] == []


def test_missing_repository_and_authentication(overview_env):
    assert overview_env['client'].get('/repositories/repo_a/overview').status_code == 401
    assert fetch(overview_env, '').status_code == 404
    assert overview_env['calls'] == []


@pytest.mark.parametrize('code', [403, 503])
def test_current_access_is_checked_before_graph_reads(overview_env, code):
    overview_env['read_error'] = code
    assert fetch(overview_env).status_code == code
    assert overview_env['calls'] == []


@pytest.mark.parametrize('code', [403, 503])
def test_unverified_settings_permission_does_not_block_overview(overview_env, code):
    overview_env['admin_error'] = code
    response = fetch(overview_env)
    assert response.status_code == 200
    assert response.json()['permissions']['can_view_settings'] is False


def test_engine_metrics_and_sections_are_explicitly_unavailable(overview_env):
    result = fetch(overview_env).json()
    unavailable = {'status': 'unavailable', 'reason': 'engine_not_implemented'}
    for name in ['decisions_at_risk', 'prs_analyzed', 'architectural_warnings']:
        assert result['kpis'][name] == {'value': None, 'availability': unavailable}
    for name in ['review_activity', 'decisions_needing_attention', 'recent_pr_reviews']:
        assert result[name]['availability'] == unavailable
        assert result[name]['items'] == []
    assert result['review_activity']['window_days'] == 7


def test_failed_indexing_preserves_successful_revision(overview_env):
    store = overview_env['store']
    with store.session_factory() as session:
        repo = session.get(RepositoryModel, 'repo_a')
        repo.tracked_branch = 'develop'
        session.commit()
    store.record_ingestion_success('repo_a', 'no-run', 'abc123456789')
    store.record_ingestion_failure('repo_a', 'no-run', 'incremental failure')
    repo = fetch(overview_env).json()['repository']
    assert repo['tracked_branch'] == 'develop'
    assert repo['indexed_commit_sha'] == 'abc123456789'
    assert repo['status'] == 'FAILED'


@pytest.mark.asyncio
async def test_graph_service_preserves_owner_guards(overview_env):
    service = overview_env['graph_service']
    with pytest.raises(RepositoryAccessDeniedError):
        await service.count_architectural_decisions('repo_b', 'usr_alice')
    with pytest.raises(RepositoryNotFoundError):
        await service.count_architectural_decisions('missing', 'usr_alice')
    assert overview_env['calls'] == []


@pytest.mark.parametrize('result', [None, ([], None, None), ([{'decision_count': None}], None, None), ([{'decision_count': -1}], None, None)])
def test_malformed_aggregate_is_not_empty_success(result):
    repository = Neo4jGraphRepository(SimpleNamespace(execute_query=lambda *a, **kw: result))
    with pytest.raises(GraphRepositoryError):
        repository.count_architectural_decisions(['repo_a'])


def test_no_driver_or_no_scope_is_an_error():
    with pytest.raises(GraphRepositoryError):
        Neo4jGraphRepository(None).count_architectural_decisions(['repo_a'])
    with pytest.raises(GraphRepositoryError):
        Neo4jGraphRepository(object()).count_architectural_decisions([])
