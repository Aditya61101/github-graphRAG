"""Shared reads use live GitHub verification, while source operations retain ownership."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_services.tests.test_github_app import env, repo
from ai_services.github_app.service import GitHubAppError
from ai_services.graph.repository import Neo4jGraphRepository
from ai_services.graph.service import RepositoryGraphService
from ai_services.graph.knowledge import RepositoryKnowledgeService
from api_services.app.utils.jwt_utils import create_access_token


@pytest.fixture
def shared(env):
    collaborator, _ = env.store.upsert_github_login(
        '2', 'bob', access_token='bob-token',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    state = dict(member=204, pull=True, identity={'id': 2, 'login': 'bob'})
    original = env.service.client._transport

    async def transport(request):
        if request.url.path == '/user':
            env.state['calls'].append(request)
            return httpx.Response(200, json=state['identity'])
        if request.url.path == '/repos/owner/repo101':
            env.state['calls'].append(request)
            value = repo()
            value['permissions'] = {'pull': state['pull'], 'admin': True}
            return httpx.Response(200, json=value)
        if request.url.path == '/repos/owner/repo101/collaborators/bob':
            env.state['calls'].append(request)
            return httpx.Response(state['member'])
        return await original.handle_async_request(request)

    env.service.client._transport = httpx.MockTransport(transport)
    calls = []
    source = dict(id='adr_a', title='Storage', source_name='storage.md',
                  source_type='MANUAL_UPLOAD', source_version='1', content_hash='hash')
    item = dict(id='decision_a', kind='decision', title='Use PostgreSQL',
                description='Persist orders in PostgreSQL.', type='technology', sources=[source])
    constraint = dict(item, id='constraint_a', kind='constraint', title=None,
                      description='Encrypt stored orders.', type='security')

    def execute(query, **params):
        calls.append((query, params))
        assert params['repo_ids'] == [state['repository'].id, '101']
        if 'decision_count' in query:
            return [dict(decision_count=1)], None, None
        if 'AS total' in query:
            return [dict(total=2)], None, None
        if 'collect(DISTINCT a' in query:
            if 'item_id' not in params:
                values = [item, constraint]
                values = [v for v in values if params['kind'] == 'all' or v['kind'] == params['kind']]
                values = [v for v in values if params['search'] in ((v['title'] or '') + v['description']).lower()]
                return values[params['offset']:params['offset'] + params['limit']], None, None
            if params['item_id'] == 'constraint_a':
                return [dict(constraint)], None, None
            if params['item_id'] != 'decision_a':
                return [], None, None
            return [dict(item)], None, None
        if 'r:AFFECTS|CONSTRAINS' in query:
            assert 'e.repository IN $repo_ids' in query
            return [dict(id='orders', name='Orders', label='Service', relationship='AFFECTS')], None, None
        if 'SUPPORTS' in query:
            for scope in ('a.repository IN $repo_ids', 'c.repository IN $repo_ids',
                          'd.repository IN $repo_ids', 'c.adr_id = a.id', 'HAS_CHUNK'):
                assert scope in query
            return [dict(id='chunk_a', adr_id='adr_a', section='Decision', chunk_index=0,
                         excerpt='Use PostgreSQL for order persistence.', truncated=False,
                         content_hash='chunk_hash')], None, None
        return [], None, None

    graph = RepositoryGraphService(env.store, Neo4jGraphRepository(SimpleNamespace(execute_query=execute)),
                                   read_authorizer=env.service.authorize_repository_read)
    env.app.state.graph_service = graph
    env.app.state.knowledge_service = RepositoryKnowledgeService(graph)
    env.app.state.adr_service = SimpleNamespace(upload=AsyncMock())
    return SimpleNamespace(env=env, user=collaborator, state=state, calls=calls,
                           headers={'Authorization': 'Bearer ' + create_access_token({'sub': collaborator.id})})


async def bind(shared):
    selected = await shared.env.service.select_repository(shared.env.user.id, 'owner/repo101')
    shared.state['repository'] = shared.env.service.bind_repository(selected, shared.env.user.id)
    return shared.state['repository']


@pytest.mark.asyncio
async def test_verified_collaborator_reads_evidence_and_overview_without_sources(shared):
    repository = await bind(shared)
    client = shared.env.client
    root = '/repositories/' + repository.id
    result = client.get(root + '/knowledge', headers=shared.headers)
    assert result.status_code == 200, result.text
    assert result.json()['items'][0]['description'] == 'Persist orders in PostgreSQL.'
    detail = client.get(root + '/knowledge/decision_a', headers=shared.headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()['evidence'][0]['excerpt'].startswith('Use PostgreSQL')
    assert detail.json()['entities'][0]['source'] == 'ADR'
    assert detail.json()['sources'][0]['content_hash'] == 'hash'
    assert 'file_path' not in detail.text and 'source_url' not in detail.text
    overview = client.get(root + '/overview', headers=shared.headers)
    assert overview.status_code == 200, overview.text
    assert overview.json()['permissions'] == dict(can_view_settings=False, can_manage_source_documents=False)
    assert client.get(root + '/graph', headers=shared.headers).status_code == 200
    assert client.get(root + '/knowledge/foreign_decision', headers=shared.headers).status_code == 404
    assert shared.env.store.get_repository(repository.id).user_id == shared.env.user.id
    membership = [r for r in shared.env.state['calls'] if '/collaborators/' in r.url.path]
    assert membership and all('opaque-installation-token' in r.headers['authorization'] for r in membership)


@pytest.mark.asyncio
@pytest.mark.parametrize('deny', ['public_unrelated', 'revoked', 'permission', 'identity', 'removed', 'suspended'])
async def test_live_revocation_and_unrelated_public_users_fail_all_read_surfaces(shared, deny):
    repository = await bind(shared)
    if deny == 'revoked':
        await shared.env.service.authorize_repository_read(shared.user.id, repository.id)
    if deny in ('public_unrelated', 'revoked'):
        shared.state['member'] = 404
    elif deny == 'permission':
        shared.state['pull'] = False
    elif deny == 'identity':
        shared.state['identity']['id'] = 999
    elif deny == 'removed':
        shared.env.state['inventory'][10] = []
    else:
        shared.env.state['installations'][10]['suspended_at'] = '2026-10-11T00:00:00Z'
    root = '/repositories/' + repository.id
    for suffix in ('/overview', '/knowledge', '/knowledge/decision_a', '/graph'):
        response = shared.env.client.get(root + suffix, headers=shared.headers)
        assert response.status_code == 403, (suffix, response.text)
    for url, body in [(root + '/query', {'query': 'Architecture?', 'conversation_id': 'shared'}),
                      ('/query', {'query': 'Architecture?', 'repository_id': repository.id,
                                  'conversation_id': 'shared'})]:
        response = shared.env.client.post(url, json=body, headers=shared.headers)
        assert response.status_code == 403, response.text
    assert shared.calls == []
    shared.env.app.state.rag_agent.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_documents_mutations_and_takeover_remain_owner_only(shared):
    repository = await bind(shared)
    root = '/repositories/' + repository.id
    assert shared.env.client.get(root + '/adrs', headers=shared.headers).status_code == 403
    response = shared.env.client.post(root + '/adrs', headers=shared.headers,
                                     files={'files': ('decision.md', b'# Decision', 'text/markdown')})
    assert response.status_code == 403
    assert shared.env.client.get(root + '/adrs', headers=shared.env.headers).status_code == 200
    assert shared.env.client.post('/repositories/owner/repo101/sync', headers=shared.headers,
                                  json={}).status_code == 403
    with pytest.raises(GitHubAppError) as exc:
        await shared.env.service.select_repository(shared.user.id, 'owner/repo101')
    assert exc.value.status_code == 409
    assert shared.env.store.get_repository(repository.id).user_id == shared.env.user.id


@pytest.mark.asyncio
async def test_chat_reads_are_scoped_to_authenticated_user_and_repository(shared):
    repository = await bind(shared)
    for headers in (shared.headers, shared.env.headers):
        response = shared.env.client.post('/repositories/' + repository.id + '/query', headers=headers,
            json={'query': 'Explain storage', 'conversation_id': 'same-conversation'})
        assert response.status_code == 200, response.text
    queries = shared.env.app.state.rag_agent.query.await_args_list
    assert queries[0].kwargs['user_id'] == shared.user.id
    assert queries[1].kwargs['user_id'] == shared.env.user.id
    assert all(q.kwargs['repository_id'] == repository.id for q in queries)
    response = shared.env.client.post('/repositories/' + repository.id + '/query', headers=shared.headers,
        json={'query': 'Explain', 'conversation_id': 'same-conversation', 'repository_id': 'foreign_repo'})
    assert response.status_code == 400
    assert shared.env.client.get('/repositories/foreign_repo/knowledge', headers=shared.headers).status_code == 404


@pytest.mark.asyncio
async def test_graph_and_knowledge_services_verify_access_independently(shared):
    repository = await bind(shared)
    shared.state['member'] = 404
    with pytest.raises(GitHubAppError):
        await shared.env.app.state.graph_service.get_repository_graph(repository.id, shared.user.id)
    with pytest.raises(GitHubAppError):
        await shared.env.app.state.knowledge_service.detail(repository.id, shared.user.id, 'decision_a')
    assert shared.calls == []


@pytest.mark.asyncio
async def test_constraints_search_pagination_and_canonical_scope(shared):
    repository = await bind(shared)
    root = '/repositories/' + repository.id
    response = shared.env.client.get(root + '/knowledge?kind=constraint&search=ENCRYPT&limit=1',
                                     headers=shared.headers)
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['id'] == 'constraint_a'
    assert shared.calls[-1][1]['search'] == 'encrypt'
    assert shared.calls[-1][1]['limit'] == 1
    assert 'SKIP $offset LIMIT $limit' in shared.calls[-1][0]
    detail = shared.env.client.get(root + '/knowledge/constraint_a', headers=shared.headers)
    assert detail.status_code == 200 and detail.json()['kind'] == 'constraint'
    owner = shared.env.client.get(root + '/overview', headers=shared.env.headers)
    assert owner.json()['permissions']['can_manage_source_documents'] is True
    assert shared.env.client.get('/repositories/101/knowledge', headers=shared.headers).status_code == 400
    for query in ('limit=51', 'offset=-1', 'kind=unknown', 'search=' + 'a' * 301):
        assert shared.env.client.get(root + '/knowledge?' + query, headers=shared.headers).status_code == 422


@pytest.mark.asyncio
async def test_non_membership_success_response_fails_closed(shared):
    repository = await bind(shared)
    shared.state['member'] = 200
    response = shared.env.client.get('/repositories/' + repository.id + '/knowledge', headers=shared.headers)
    assert response.status_code == 502
    assert shared.calls == []
