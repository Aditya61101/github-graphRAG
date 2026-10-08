from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from ai_services.github_app.service import GitHubAppService, GitHubAppError
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.persistence.models import Base, RepositoryModel, GitHubConnectionModel, GitHubInstallationModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.persistence.migrate_github_app import upgrade
from ai_services.ingestion.service import IngestionResult, RepositoryIngestionService
from ai_services.ingestion.sources.interface import RepositoryRef, RepositoryMetadata
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from api_services.app.routers.auth import router as auth_router, GitHubOAuth
from api_services.app.routers.repositories import router as repository_router
from api_services.app.routers.query import router as query_router
from api_services.app.routers.webhook import router as webhook_router
from api_services.app.utils.jwt_utils import create_access_token, decode_access_token


def repo(rid=101, private=False):
    return {'id': rid, 'name': f'repo{rid}', 'full_name': f'owner/repo{rid}',
            'owner': {'login': 'owner'}, 'private': private, 'html_url': f'https://github.com/owner/repo{rid}',
            'default_branch': 'main'}


def installation(iid=10, **overrides):
    return dict({'id': iid, 'app_id': 123, 'account': {'id': 50, 'login': 'owner', 'type': 'Organization'},
                 'repository_selection': 'selected', 'suspended_at': None}, **overrides)


@pytest.fixture
def env(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('GITHUB_TOKEN_ENCRYPTION_KEY', key)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = SqliteApplicationStore(sessionmaker(bind=engine, expire_on_commit=False))
    user, connection = store.upsert_github_login('1', 'alice', access_token='opaque-user-token',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    settings = GitHubAppSettings('123', 'decisionguard', 'client', 'client-secret', pem,
        'webhook-secret', 'http://localhost/auth/github/callback', 'http://localhost:3000', 's' * 40, key)
    state = {'installations': {10: installation()}, 'inventory': {10: [repo()]},
             'user_installations': [installation()], 'user_repositories': {10: [repo()]},
             'calls': [], 'tokens': {}, 'identity': {'id': 1, 'login': 'alice'}, 'user_status': 200}

    def transport(request):
        state['calls'].append(request)
        path = request.url.path
        page = int(request.url.params.get('page', '1'))
        def sliced(items): return items[(page - 1) * 100:page * 100]
        if path == '/user': return httpx.Response(state['user_status'], json=state['identity'])
        if path == '/user/installations':
            return httpx.Response(state['user_status'], json={'installations': sliced(state['user_installations'])})
        if path.startswith('/user/installations/'):
            iid = int(path.split('/')[3])
            return httpx.Response(state['user_status'], json={'repositories': sliced(state['user_repositories'].get(iid, []))})
        if path.startswith('/app/installations/'):
            iid = int(path.split('/')[3])
            value = state['installations'].get(iid)
            if value is None: return httpx.Response(404, json={})
            if path.endswith('/access_tokens'):
                token = f'opaque-installation-token-{iid}-{len(state["calls"])}'
                state['tokens'][token] = iid
                return httpx.Response(201, json={'token': token, 'expires_at':
                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            return httpx.Response(200, json=value)
        if path == '/installation/repositories':
            token = request.headers['authorization'].removeprefix('Bearer ')
            iid = state['tokens'][token]
            return httpx.Response(200, json={'repositories': sliced(state['inventory'][iid])})
        raise AssertionError(f'Unexpected mocked GitHub request: {path}')

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    service = GitHubAppService(store, settings, http)
    oauth = GitHubOAuth(settings)
    oauth.client.fetch_access_token = AsyncMock(return_value={'access_token': 'opaque-user-token', 'expires_in': 3600})
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    app.include_router(auth_router, prefix='/auth')
    app.include_router(repository_router, prefix='/repositories')
    app.include_router(query_router, prefix='/query')
    app.include_router(webhook_router, prefix='/webhooks')
    app.state.sqlite_store, app.state.github_app, app.state.github_oauth = store, service, oauth
    app.state.rag_agent = SimpleNamespace(query=AsyncMock(return_value=RAGAgentResult(answer='grounded', sources={})))
    app.state.ingestion_service = SimpleNamespace(
        ingest_repository=AsyncMock(return_value=IngestionResult('owner/repo101', 'repo_101', 'completed', 'sha', False)),
        handle_github_push_webhook=AsyncMock(return_value=IngestionResult('owner/repo101', 'repo_101', 'completed', 'sha', True)))
    client = TestClient(app, follow_redirects=False)
    yield SimpleNamespace(store=store, user=user, connection=connection, settings=settings, state=state,
        service=service, oauth=oauth, app=app, client=client,
        headers={'Authorization': 'Bearer ' + create_access_token({'sub': user.id})}, engine=engine)
    client.close()
    engine.dispose()


def begin_login(env):
    response = env.client.get('/auth/github/login')
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers['location']).query)


def callback(env, state, **params):
    return env.client.get('/auth/github/callback', params={'state': state, 'code': 'code', **params})


def webhook(env, event, payload, signature=True):
    raw = json.dumps(payload).encode()
    digest = hmac.new(env.settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return env.client.post('/webhooks/github', content=raw, headers={
        'X-GitHub-Event': event, 'X-Hub-Signature-256': 'sha256=' + (digest if signature else 'invalid')})


@pytest.mark.asyncio
async def track(env, rid=101):
    selected = await env.service.select_repository(env.user.id, f'owner/repo{rid}')
    return env.service.bind_repository(selected, env.user.id)


def test_identity_is_numeric_not_username_and_tokens_are_encrypted(env):
    again, _ = env.store.upsert_github_login('1', 'renamed', access_token='new-token',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    other, connection = env.store.upsert_github_login('2', 'renamed', access_token='other-token',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert again.id == env.user.id and other.id != again.id
    assert connection.access_token.startswith('enc:v1:') and 'other-token' not in connection.access_token
    assert env.store.get_credential_for_user(other.id).token == 'other-token'
    claims = decode_access_token(create_access_token({'sub': again.id, 'username': 'ignored'}))
    assert set(claims) == {'sub', 'exp'}


def test_login_pkce_fixed_callback_and_existing_installation(env):
    params = begin_login(env)
    assert params['redirect_uri'] == [env.settings.callback_url]
    assert params['code_challenge_method'] == ['S256'] and params['code_challenge']
    assert 'scope' not in params
    response = callback(env, params['state'][0])
    assert response.status_code == 307
    assert parse_qs(urlparse(response.headers['location']).query)['next'] == ['/dashboard']
    me = env.client.get('/auth/me', headers=env.headers).json()
    assert not me['installation_onboarding_required'] and not me['reconnect_required']
    assert 'access_token' not in json.dumps(me)


def test_invalid_state_never_exchanges_code_and_replay_is_rejected(env):
    assert callback(env, 'unknown').status_code == 400
    env.oauth.client.fetch_access_token.assert_not_awaited()
    state = begin_login(env)['state'][0]
    assert callback(env, state).status_code == 307
    assert callback(env, state).status_code == 400
    assert callback(env, state, installation_id=10).status_code == 400
    assert env.oauth.client.fetch_access_token.await_count == 1


def test_missing_local_transaction_restarts_protected_oauth(env):
    response = env.client.get('/auth/github/callback', params={'code': 'direct', 'installation_id': '10'})
    assert response.status_code == 302
    assert urlparse(response.headers['location']).netloc == 'github.com'
    env.oauth.client.fetch_access_token.assert_not_awaited()


def test_state_browser_binding_and_cancellation(env):
    state = begin_login(env)['state'][0]
    with TestClient(env.app, follow_redirects=False) as other:
        assert other.get('/auth/github/callback', params={'state': state, 'code': 'code'}).status_code == 400
    state = begin_login(env)['state'][0]
    assert env.client.get('/auth/github/callback', params={'state': state, 'error': 'access_denied'}).status_code == 400
    env.oauth.client.fetch_access_token.assert_not_awaited()


@pytest.mark.parametrize('webhook_first', [True, False])
def test_installation_completion_before_or_after_webhook(env, webhook_first):
    if webhook_first:
        assert webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}}).status_code == 200
    response = env.client.post('/auth/github/install', headers=env.headers)
    state = parse_qs(urlparse(response.json()['installation_url']).query)['state'][0]
    assert callback(env, state, installation_id='10', setup_action='install').status_code == 307
    if not webhook_first:
        assert webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}}).status_code == 200
    assert env.store.list_repositories(env.user.id) == []  # Never assign ownership from sender.


def test_installation_hint_and_account_switch_cannot_grant_access(env):
    response = env.client.post('/auth/github/install', headers=env.headers)
    state = parse_qs(urlparse(response.json()['installation_url']).query)['state'][0]
    assert callback(env, state, installation_id='999').status_code == 403
    response = env.client.post('/auth/github/install', headers=env.headers)
    state = parse_qs(urlparse(response.json()['installation_url']).query)['state'][0]
    env.state['identity'] = {'id': 2, 'login': 'bob'}
    assert callback(env, state, installation_id='10').status_code == 403
    assert env.store.get_user_github_connection(env.user.id).github_user_id == '1'


def test_no_usable_installation_requires_onboarding(env):
    env.state['user_installations'] = []
    me = env.client.get('/auth/me', headers=env.headers).json()
    assert me['installation_onboarding_required'] and not me['reconnect_required']


@pytest.mark.asyncio
async def test_pagination_public_only_discovery_and_stale_rows(env):
    env.state['inventory'][10] = [repo(rid) for rid in range(100, 205)] + [repo(300, True)]
    env.state['user_repositories'][10] = env.state['inventory'][10]
    summaries, accessible = await env.service.discovery(env.user.id)
    assert len(accessible) == 105 and all(not item['private'] for item in accessible)
    assert any(request.url.params.get('page') == '2' for request in env.state['calls'])
    selected = next(item for item in accessible if item['id'] == 101)
    env.service.bind_repository(selected, env.user.id)
    env.state['inventory'][10] = []
    response = env.client.get('/repositories', headers=env.headers)
    assert response.status_code == 200 and response.json() == []
    assert env.client.get('/repositories/tracked', headers=env.headers).json() == []


def test_unselected_public_repository_and_other_installation_hint_rejected(env):
    for body in [{'repository': 'owner/unselected'}, {'repository': 'owner/repo101', 'installation_id': '999'}]:
        response = env.client.post('/repositories/ingest', headers=env.headers, json=body)
        assert response.status_code == 403
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


def test_manual_job_passes_ids_not_user_credentials(env):
    response = env.client.post('/repositories/ingest', headers=env.headers,
                              json={'repository': 'owner/repo101'})
    assert response.status_code == 202
    call = env.app.state.ingestion_service.ingest_repository.call_args.kwargs
    assert call['repository_id'] == 'repo_101' and call['installation_id'] == '10'
    assert 'credential' not in call and 'connection_id' not in call


@pytest.mark.asyncio
async def test_indexing_uses_narrow_installation_token_not_user_token(env):
    tracked = await track(env)
    source = SimpleNamespace(resolve_ref=lambda identifier, branch=None: RepositoryRef.parse(identifier, branch),
        get_metadata=AsyncMock(return_value=RepositoryMetadata('101', 'owner', 'repo101', 'owner/repo101',
                                                             'main', 'main', 'https://github.com/owner/repo101')))
    pipeline = RepositoryIngestionService(source, env.store, None, github_app=env.service)
    pipeline._run_full_ingestion = AsyncMock(return_value=IngestionResult('owner/repo101', tracked.id, 'completed', 'sha', False))
    await pipeline.ingest_repository(tracked.id, repository_id=tracked.id, installation_id='10', user_id=env.user.id)
    credential = source.get_metadata.call_args.kwargs['credential']
    assert credential.token.startswith('opaque-installation-token')
    mint = [request for request in env.state['calls'] if request.method == 'POST'][-1]
    assert json.loads(mint.content)['repository_ids'] == [101]
    env.state['user_status'] = 401
    with pytest.raises(GitHubAppError):
        await pipeline.ingest_repository(tracked.id, repository_id=tracked.id, installation_id='10', user_id=env.user.id,
                                         credential=GitHubCredential('legacy-token'))


@pytest.mark.asyncio
async def test_token_expiration_scope_cache_and_access_invalidation(env):
    await track(env)
    first = await env.service.installation_token('10', ('101',))
    assert (await env.service.installation_token('10', ('101',))).token == first.token
    assert (await env.service.installation_token('10', ('102',))).token != first.token
    key = next(key for key in env.service.tokens if key[1] == ('101',))
    env.service.tokens[key] = (first.token, datetime.now(timezone.utc))
    assert (await env.service.installation_token('10', ('101',))).token != first.token
    env.state['inventory'][10] = []
    await env.service.reconcile('10')
    assert not any(key[1] == ('101',) for key in env.service.tokens)
    with pytest.raises(GitHubAppError):
        await env.service.indexing_credential('repo_101', '10')


@pytest.mark.parametrize('change,action,event', [('removed', 'removed', 'installation_repositories'),
    ('suspended', 'suspend', 'installation'), ('deleted', 'deleted', 'installation')])
def test_access_lifecycle_blocks_operations_and_late_created_does_not_reactivate(env, change, action, event):
    env.client.post('/repositories/ingest', headers=env.headers, json={'repository': 'owner/repo101', 'sync': True})
    if change == 'removed': env.state['inventory'][10] = []
    if change == 'suspended': env.state['installations'][10]['suspended_at'] = '2026-01-01T00:00:00Z'
    if change == 'deleted': env.state['installations'][10] = None
    assert webhook(env, event, {'action': action, 'installation': {'id': 10}}).status_code == 200
    assert webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}}).status_code == 200
    for path in ['/repositories/owner/repo101', '/repositories/repo_101/graph', '/repositories/repo_101/adrs']:
        assert env.client.get(path, headers=env.headers).status_code == 403


def test_expired_user_connection_requires_reconnect(env):
    with env.store.session_factory() as session:
        connection = session.get(GitHubConnectionModel, env.connection.id)
        connection.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    me = env.client.get('/auth/me', headers=env.headers).json()
    assert me['reconnect_required']
    assert env.client.get('/repositories', headers=env.headers).status_code == 401


def test_revocation_disables_user_not_installation(env):
    webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}})
    env.state['user_status'] = 401
    assert webhook(env, 'github_app_authorization', {'action': 'revoked', 'sender': {'id': 1}}).status_code == 200
    assert env.store.get_user_github_connection(env.user.id).access_status == 'RECONNECT_REQUIRED'
    with env.store.session_factory() as session:
        assert session.get(GitHubInstallationModel, '10').status == 'ACTIVE'


def test_ping_signature_and_malformed_payloads(env):
    del env.app.state.ingestion_service
    assert webhook(env, 'ping', {}).status_code == 200
    assert webhook(env, 'ping', {}, signature=False).status_code == 401
    assert webhook(env, 'ping', []).status_code == 400
    assert webhook(env, 'installation', {'installation': []}).status_code == 400


def test_missing_query_scope_and_conversation_isolation(env):
    env.client.post('/repositories/ingest', headers=env.headers, json={'repository': 'owner/repo101'})
    assert env.client.post('/query', headers=env.headers, json={'query': 'Q', 'conversation_id': 'same'}).status_code == 422
    payload = {'query': 'Q', 'conversation_id': 'same', 'repository_id': 'repo_101'}
    assert env.client.post('/query', headers=env.headers, json=payload).status_code == 200
    assert env.app.state.rag_agent.query.call_args.kwargs['user_id'] == env.user.id
    env.service.bind_conversation('another-user', 'repo_101', 'same')
    with pytest.raises(GitHubAppError, match='different repository'):
        env.service.bind_conversation(env.user.id, 'repo_102', 'same')


def test_sqlite_upgrade_preserves_existing_ids_and_adr_foreign_keys(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, email TEXT, avatar_url TEXT, created_at DATETIME, updated_at DATETIME)')
        conn.exec_driver_sql('CREATE TABLE github_connections (id TEXT PRIMARY KEY, user_id TEXT, github_user_id TEXT, access_token TEXT, token_type TEXT, scope TEXT, created_at DATETIME, updated_at DATETIME)')
        conn.exec_driver_sql('CREATE TABLE repositories (id TEXT PRIMARY KEY, github_repository_id TEXT, owner TEXT, name TEXT, full_name TEXT, repository_url TEXT, default_branch TEXT, tracked_branch TEXT, user_id TEXT, github_connection_id TEXT, indexed_commit_sha TEXT, status TEXT, created_at DATETIME, updated_at DATETIME)')
        conn.exec_driver_sql('CREATE TABLE adrs (id TEXT PRIMARY KEY, repository_id TEXT REFERENCES repositories(id), title TEXT)')
        conn.exec_driver_sql("INSERT INTO users VALUES ('user-old', 'alice', NULL, NULL, NULL, NULL)")
        conn.exec_driver_sql("INSERT INTO github_connections VALUES ('connection-old', 'user-old', '1', 'plaintext', 'Bearer', NULL, NULL, NULL)")
        conn.exec_driver_sql("INSERT INTO repositories VALUES ('repo_101','101','owner','repo101','owner/repo101','url','main','main','user-old','connection-old','sha','COMPLETED',NULL,NULL)")
        conn.exec_driver_sql("INSERT INTO adrs VALUES ('adr-old','repo_101','Decision')")
    key = Fernet.generate_key().decode()
    upgrade(engine, key)
    upgrade(engine, key)
    with engine.connect() as conn:
        row = conn.execute(text('SELECT id, installation_id, access_state FROM repositories')).one()
        assert tuple(row) == ('repo_101', None, 'RECONNECT_REQUIRED')
        assert conn.execute(text('SELECT repository_id FROM adrs')).scalar() == 'repo_101'
        stored = conn.execute(text('SELECT access_token FROM github_connections')).scalar()
        assert Fernet(key.encode()).decrypt(stored.removeprefix('enc:v1:').encode()) == b'plaintext'
    engine.dispose()


def test_settings_validate_required_values_and_rsa_key(env, monkeypatch, tmp_path):
    names = ['GITHUB_APP_ID', 'GITHUB_APP_SLUG', 'GITHUB_APP_CLIENT_ID', 'GITHUB_APP_CLIENT_SECRET',
             'GITHUB_APP_PRIVATE_KEY_PATH', 'GITHUB_APP_WEBHOOK_SECRET', 'GITHUB_APP_CALLBACK_URL',
             'FRONTEND_URL', 'OAUTH_SESSION_SECRET', 'GITHUB_TOKEN_ENCRYPTION_KEY']
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match='Missing GitHub App configuration'):
        GitHubAppSettings.from_env()
    key_path = tmp_path / 'app.pem'
    key_path.write_bytes(env.settings.private_key)
    values = ['123', 'decisionguard', 'client', 'secret', str(key_path), 'webhook-secret',
              env.settings.callback_url, env.settings.frontend_url, 's' * 40, env.settings.encryption_key]
    for name, value in zip(names, values): monkeypatch.setenv(name, value)
    settings = GitHubAppSettings.from_env()
    assert settings.app_id == '123'
    assert 'webhook-secret' not in repr(settings)
    key_path.write_bytes(b'not an RSA key')
    with pytest.raises(ValueError, match='RSA PEM'):
        GitHubAppSettings.from_env()


def test_expired_state_and_provider_authorization_failure(env):
    from dataclasses import replace
    state = begin_login(env)['state'][0]
    env.oauth.transactions[state] = replace(env.oauth.transactions[state], expires_at=0)
    assert callback(env, state).status_code == 400
    env.oauth.client.fetch_access_token.assert_not_awaited()
    state = begin_login(env)['state'][0]
    env.oauth.client.fetch_access_token.side_effect = RuntimeError('secret-provider-detail')
    response = callback(env, state)
    assert response.status_code == 400 and 'secret-provider-detail' not in response.text


def test_pending_installation_does_not_become_active(env):
    env.state['user_installations'] = []
    started = env.client.post('/auth/github/install', headers=env.headers)
    state = parse_qs(urlparse(started.json()['installation_url']).query)['state'][0]
    assert callback(env, state, installation_id='20', setup_action='install').status_code == 403
    with env.store.session_factory() as session:
        assert session.get(GitHubInstallationModel, '20') is None


@pytest.mark.asyncio
async def test_ownership_conflict_and_another_user_installation_selection(env):
    await track(env)
    other, _ = env.store.upsert_github_login('2', 'bob', access_token='bob-token',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(GitHubAppError) as error:
        await env.service.select_repository(other.id, 'owner/repo101')
    assert error.value.status_code == 409
    env.state['installations'][20] = installation(20)
    env.state['inventory'][20] = [repo(102)]
    with pytest.raises(GitHubAppError):
        await env.service.select_repository(env.user.id, 'owner/repo102', '20')


@pytest.mark.asyncio
async def test_webhook_indexing_uses_installation_credentials_without_user_token(env):
    tracked = await track(env)
    env.service.revoke_user('1')
    source = SimpleNamespace(resolve_ref=lambda value, branch=None: RepositoryRef.parse(value, branch),
        get_metadata=AsyncMock(return_value=RepositoryMetadata('101', 'owner', 'repo101', 'owner/repo101',
                                                             'main', 'main', 'https://github.com/owner/repo101')))
    pipeline = RepositoryIngestionService(source, env.store, None, github_app=env.service)
    pipeline._run_full_ingestion = AsyncMock(return_value=IngestionResult('owner/repo101', tracked.id, 'completed', 'sha', False))
    payload = {'repository': repo(), 'installation': {'id': 10}, 'ref': 'refs/heads/main', 'after': 'a' * 40}
    await pipeline.handle_github_push_webhook(payload)
    assert source.get_metadata.call_args.kwargs['credential'].token.startswith('opaque-installation-token')
    source.get_metadata.reset_mock()
    payload['installation']['id'] = 20
    assert (await pipeline.handle_github_push_webhook(payload)).status == 'ignored'
    source.get_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_guard_blocks_revoked_access_before_graph_write(env, tmp_path):
    from ai_services.tests.test_ingestion_coverage import pipeline, plan
    from unittest.mock import MagicMock
    tracked = await track(env)
    (tmp_path / 'auth.py').write_text('def login():\n    return True\n')
    knowledge = SimpleNamespace(process_chunks=AsyncMock(return_value={
        'chunks': [], 'candidate_knowledge': [], 'entities': SimpleNamespace(entities={}),
        'relationship_candidates': [],
    }))
    instance = pipeline(tmp_path, knowledge_pipeline=knowledge,
                        publication_guard=lambda: env.service.indexing_credential(tracked.id, '10'))
    env.state['inventory'][10] = []
    with pytest.raises(GitHubAppError):
        await instance.ingest(plan(['auth.py']).files)
    instance.neo4j_writer.write_source.assert_not_called()
    instance.neo4j_writer.write_entities.assert_not_called()


@pytest.mark.asyncio
async def test_agent_checkpoint_scope_includes_user_and_repository():
    from ai_services.agents.rag_agent.agent import RAGQueryAgent
    from langchain_core.messages import AIMessage
    agent = RAGQueryAgent.__new__(RAGQueryAgent)
    agent.agent = SimpleNamespace(ainvoke=AsyncMock(return_value={'messages': [AIMessage(content='answer')]}))
    for user, repository in [('alice', 'repo1'), ('bob', 'repo1'), ('alice', 'repo2')]:
        await agent.query('same', 'Q', repository_id=repository, user_id=user)
    keys = [call.args[1]['configurable']['thread_id'] for call in agent.agent.ainvoke.call_args_list]
    assert len(set(keys)) == 3
    with pytest.raises(ValueError, match='authorized repository'):
        await agent.query('same', 'Q', user_id='alice')


@pytest.mark.asyncio
async def test_model_cannot_supply_or_override_repository_scope(monkeypatch):
    from ai_services.agents.rag_agent.tools.query_tool import create_query_graph_rag_tool
    retrieval = AsyncMock()
    monkeypatch.setattr('ai_services.agents.rag_agent.tools.query_tool.hybrid_retrieve', retrieval)
    tool = create_query_graph_rag_tool(None, None, None, None, None, None, None)
    for state, argument in [({}, 'repo101'), ({'repository_id': 'repo101'}, 'repo102')]:
        runtime = SimpleNamespace(state=state, tool_call_id='test')
        with pytest.raises(ValueError):
            await tool.coroutine(query='Q', runtime=runtime, repository_id=argument)
    retrieval.assert_not_awaited()


def test_git_credentials_are_transient_and_do_not_fall_back(monkeypatch, tmp_path):
    import base64
    import subprocess
    from unittest.mock import Mock
    from ai_services.ingestion.sources.github import GitHubRepositorySource
    from ai_services.ingestion.sources.credentials import GitHubCredential
    source = GitHubRepositorySource(tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setattr('ai_services.ingestion.sources.github.subprocess.run', run)
    monkeypatch.setenv('GIT_ASKPASS', 'old-user-token-helper')
    credential = GitHubCredential(token='opaque-secret')
    source._run_git(['fetch', 'origin'], credential)
    args, kwargs = run.call_args
    assert args[0] == ['git', 'fetch', 'origin']
    assert credential.token not in str(args)
    config = kwargs['env']
    assert config['GIT_TERMINAL_PROMPT'] == '0'
    assert config['GIT_ASKPASS'] == config['SSH_ASKPASS'] == ''
    assert config['GIT_CONFIG_KEY_0'] == 'credential.helper'
    assert config['GIT_CONFIG_VALUE_0'] == ''
    assert config['GIT_CONFIG_KEY_1'] == 'http.extraheader'
    assert config['GIT_CONFIG_VALUE_1'] == ''
    encoded = base64.b64encode(b'x-access-token:opaque-secret').decode()
    assert config['GIT_CONFIG_VALUE_2'] == f'Authorization: Basic {encoded}'
    assert source._safe_git_error(f'{credential.token} {encoded}', credential) == '[REDACTED] [REDACTED]'
    source._run_git(['rev-parse', 'HEAD'])
    assert run.call_args.kwargs['env']['GIT_CONFIG_COUNT'] == '2'


def test_permissions_webhook_invalidates_tokens_even_with_unchanged_inventory(env):
    assert webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}}).status_code == 200
    # First reconciliation changes inventory and clears its discovery token.
    assert webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}}).status_code == 200
    tokens_before = {value[0] for value in env.service.tokens.values()}
    assert tokens_before
    response = webhook(env, 'installation', {'action': 'new_permissions_accepted', 'installation': {'id': 10}})
    assert response.status_code == 200
    assert tokens_before.isdisjoint({value[0] for value in env.service.tokens.values()})


@pytest.mark.asyncio
async def test_git_auth_failure_preserves_existing_clone(monkeypatch, tmp_path):
    import subprocess
    from ai_services.ingestion.sources.github import GitHubRepositorySource
    from ai_services.ingestion.sources.credentials import GitHubCredential
    from ai_services.ingestion.sources.interface import RepositoryAuthenticationError
    source = GitHubRepositorySource(tmp_path)
    clone = tmp_path / 'owner' / 'repo101'
    (clone / '.git').mkdir(parents=True)
    source.get_metadata = AsyncMock(return_value=RepositoryMetadata(
        '101', 'owner', 'repo101', 'owner/repo101', 'main', 'main', 'https://github.com/owner/repo101'))
    monkeypatch.setattr(source, '_run_git', lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 128, '', 'Authentication failed: 403'))
    with pytest.raises(RepositoryAuthenticationError):
        await source.prepare_snapshot(RepositoryRef.parse('owner/repo101'),
                                      credential=GitHubCredential(token='opaque-secret'))
    assert (clone / '.git').is_dir()
