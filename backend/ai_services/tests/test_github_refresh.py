from datetime import datetime, timedelta, timezone
import asyncio
from unittest.mock import Mock, AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, text, inspect

from ai_services.tests.test_github_app import env, track, begin_login, callback, webhook
from ai_services.github_app.service import GitHubAppError, as_utc
from ai_services.ingestion.persistence.models import GitHubConnectionModel
from ai_services.ingestion.persistence.migrate_github_refresh import upgrade


def credentials(env, seconds=-1, refresh_seconds=3600):
    with env.store.session_factory() as session:
        connection = session.get(GitHubConnectionModel, env.connection.id)
        connection.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        connection.refresh_token = env.store.encrypt_token('original-refresh')
        connection.refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=refresh_seconds)
        session.commit()
    return env.store.get_user_github_connection(env.user.id)


def refreshing(env, status=200, body=None, error=None):
    calls = []
    async def transport(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        if error:
            raise error
        return httpx.Response(status, json=body if body is not None else {
            'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
            'expires_in': 28800, 'refresh_token_expires_in': 15897600})
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    env.service.client = client
    return client, calls


@pytest.mark.asyncio
async def test_valid_token_reused(env):
    credentials(env, seconds=3600)
    client, calls = refreshing(env)
    async with client:
        assert await env.service.user_token(env.user.id) == 'opaque-user-token'
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('seconds', [-10, 30])
async def test_expired_and_near_expiry_rotate_encrypted_credentials(env, seconds):
    credentials(env, seconds=seconds)
    before = datetime.now(timezone.utc)
    client, calls = refreshing(env)
    async with client:
        assert await env.service.user_token(env.user.id) == 'rotated-access'
        assert await env.service.user_token(env.user.id) == 'rotated-access'
    assert len(calls) == 1
    assert calls[0].url == 'https://github.com/login/oauth/access_token'
    assert b'grant_type=refresh_token' in calls[0].content
    assert b'client_secret=client-secret' in calls[0].content
    connection = env.store.get_user_github_connection(env.user.id)
    assert connection.access_token.startswith('enc:v1:')
    assert connection.refresh_token.startswith('enc:v1:')
    assert env.store.decrypt_token(connection.refresh_token) == 'rotated-refresh'
    assert before + timedelta(seconds=28800) <= as_utc(connection.access_token_expires_at)
    assert before + timedelta(seconds=15897600) <= as_utc(connection.refresh_token_expires_at)
    assert 'refresh_token' not in connection.to_dict()


@pytest.mark.asyncio
async def test_simultaneous_requests_refresh_once(env):
    credentials(env)
    client, calls = refreshing(env)
    async with client:
        result = await asyncio.gather(*(env.service.user_token(env.user.id) for _ in range(10)))
    assert result == ['rotated-access'] * 10
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_refresh_requires_reauthorization_preserves_installations(env):
    await track(env)
    credentials(env, refresh_seconds=-1)
    client, calls = refreshing(env)
    async with client:
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_token(env.user.id)
    assert exc.value.reconnect_required and exc.value.status_code == 401
    assert calls == []
    assert env.store.get_repository('repo_101').installation_id == '10'


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['bad_refresh_token', 'invalid_grant', 'access_denied'])
async def test_explicit_refresh_rejection_requires_reauthorization(env, error):
    credentials(env)
    client, _ = refreshing(env, body={'error': error, 'error_description': 'private-provider-detail'})
    async with client:
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_token(env.user.id)
    assert exc.value.reconnect_required
    assert 'private-provider-detail' not in str(exc.value)
    assert env.store.get_user_github_connection(env.user.id).access_status == 'RECONNECT_REQUIRED'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,error', [
    (503, {}, None), (429, {}, None), (403, {}, None),
    (200, {'error': 'incorrect_client_credentials'}, None),
    (200, {'access_token': 'partial-response'}, None),
    (200, None, httpx.ReadTimeout('private-provider-detail')),
])
async def test_transient_or_configuration_failure_preserves_credentials(env, status, body, error):
    original = credentials(env)
    client, _ = refreshing(env, status, body, error)
    async with client:
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_token(env.user.id)
    assert not exc.value.reconnect_required
    connection = env.store.get_user_github_connection(env.user.id)
    assert connection.access_token == original.access_token
    assert connection.refresh_token == original.refresh_token
    assert connection.access_status == 'ACTIVE'
    assert 'private-provider-detail' not in str(exc.value)


@pytest.mark.asyncio
async def test_persistence_failure_never_returns_rotated_token(env, monkeypatch):
    original = credentials(env)
    monkeypatch.setattr(env.store, 'rotate_github_tokens', Mock(side_effect=RuntimeError('secret-details')))
    client, _ = refreshing(env)
    async with client:
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_token(env.user.id)
    assert exc.value.status_code == 503 and not exc.value.reconnect_required
    assert 'secret-details' not in str(exc.value)
    assert env.store.get_user_github_connection(env.user.id).refresh_token == original.refresh_token


def test_me_discovers_installations_after_refresh(env):
    credentials(env)
    original = env.service.client
    refreshes = []
    async def transport(request):
        if request.url.host == 'github.com':
            refreshes.append(request)
            return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                'expires_in': 28800, 'refresh_token_expires_in': 15897600})
        if request.url.path.startswith('/user/'):
            assert request.headers['Authorization'] == 'Bearer rotated-access'
        return await original.send(request)
    env.service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    response = env.client.get('/auth/me', headers=env.headers)
    assert response.status_code == 200
    assert response.json()['installations'][0]['id'] == '10'
    assert not response.json()['reconnect_required']
    assert not response.json()['installation_onboarding_required']
    assert len(refreshes) == 1
    assert 'rotated-' not in response.text


@pytest.mark.parametrize('status', [403, 429, 503])
def test_discovery_failure_is_not_an_empty_installation_result(env, status):
    env.state['user_status'] = status
    response = env.client.get('/auth/me', headers=env.headers)
    assert response.status_code == (403 if status == 403 else 503)
    assert 'installations' not in response.json()
    assert env.store.get_user_github_connection(env.user.id).access_status == 'ACTIVE'


def test_installation_auth_failure_does_not_require_user_reconnection(env):
    env.service.reconcile = AsyncMock(side_effect=GitHubAppError(401, 'Installation authentication failed'))
    response = env.client.get('/auth/me', headers=env.headers)
    assert response.status_code == 502
    assert 'reconnect_required' not in response.json()
    assert env.store.get_user_github_connection(env.user.id).access_status == 'ACTIVE'
    assert env.client.get('/repositories', headers=env.headers).status_code == 502


def test_login_persists_refresh_credentials(env):
    state = begin_login(env)['state'][0]
    assert callback(env, state).status_code == 307
    connection = env.store.get_user_github_connection(env.user.id)
    assert env.store.decrypt_token(connection.refresh_token) == 'login-refresh-token'
    assert as_utc(connection.refresh_token_expires_at) > datetime.now(timezone.utc)


def test_version_two_upgrade_is_additive_and_repeatable(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "refresh.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
        conn.exec_driver_sql('INSERT INTO app_schema_version VALUES (1)')
        conn.exec_driver_sql('CREATE TABLE github_connections (id TEXT PRIMARY KEY, access_token TEXT)')
        conn.execute(text('INSERT INTO github_connections VALUES (:id, :token)'), {'id': 'same', 'token': 'encrypted'})
    upgrade(engine)
    upgrade(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() == 2
        assert conn.exec_driver_sql('SELECT id, access_token, refresh_token FROM github_connections').one() == ('same', 'encrypted', None)
        assert 'refresh_token_expires_at' in {c['name'] for c in inspect(conn).get_columns('github_connections')}
    engine.dispose()


@pytest.mark.asyncio
async def test_database_recovery_persists_pending_rotation_without_refreshing_twice(env, monkeypatch):
    credentials(env)
    persist = env.store.rotate_github_tokens
    attempts = []
    def flaky_persistence(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError('Database temporarily unavailable')
        return persist(*args)
    monkeypatch.setattr(env.store, 'rotate_github_tokens', flaky_persistence)
    client, calls = refreshing(env)
    async with client:
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_token(env.user.id)
        assert exc.value.status_code == 503
        assert await env.service.user_token(env.user.id) == 'rotated-access'
    assert len(calls) == 1 and len(attempts) == 2
    assert env.service.pending_rotations == {}


@pytest.mark.asyncio
async def test_new_login_supersedes_pending_rotation(env, monkeypatch):
    credentials(env)
    persist = env.store.rotate_github_tokens
    monkeypatch.setattr(env.store, 'rotate_github_tokens', Mock(side_effect=RuntimeError('Database unavailable')))
    client, calls = refreshing(env)
    async with client:
        with pytest.raises(GitHubAppError):
            await env.service.user_token(env.user.id)
        env.store.upsert_github_login('1', 'alice', access_token='new-login',
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            refresh_token='new-login-refresh', refresh_expires_at=datetime.now(timezone.utc) + timedelta(days=180))
        monkeypatch.setattr(env.store, 'rotate_github_tokens', persist)
        assert await env.service.user_token(env.user.id) == 'new-login'
    assert len(calls) == 1 and env.service.pending_rotations == {}


def test_rotation_rolls_back_when_commit_fails(env, monkeypatch):
    original = credentials(env)
    from sqlalchemy.orm import Session
    def fail_commit(session):
        raise RuntimeError('Database commit failed')
    monkeypatch.setattr(Session, 'commit', fail_commit)
    with pytest.raises(RuntimeError, match='commit failed'):
        env.store.rotate_github_tokens(original, 'rotated-access', 'rotated-refresh',
            datetime.now(timezone.utc) + timedelta(hours=8), datetime.now(timezone.utc) + timedelta(days=180))
    stored = env.store.get_user_github_connection(env.user.id)
    assert stored.access_token == original.access_token
    assert stored.refresh_token == original.refresh_token
    assert stored.access_token_expires_at == original.access_token_expires_at
    assert stored.refresh_token_expires_at == original.refresh_token_expires_at


def test_rotation_cannot_overwrite_revocation(env):
    original = credentials(env)
    env.service.revoke_user('1')
    with pytest.raises(RuntimeError, match='authorization changed'):
        env.store.rotate_github_tokens(original, 'rotated-access', 'rotated-refresh',
            datetime.now(timezone.utc) + timedelta(hours=8), datetime.now(timezone.utc) + timedelta(days=180))
    assert env.store.get_user_github_connection(env.user.id).access_status == 'RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_simultaneous_user_api_401s_refresh_once_and_retry(env):
    credentials(env, seconds=3600)
    refreshes, rejections = [], []
    both_rejected = asyncio.Event()
    async def transport(request):
        if request.url.host == 'github.com':
            refreshes.append(request)
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                'expires_in': 28800, 'refresh_token_expires_in': 15897600})
        if request.headers['Authorization'] == 'Bearer opaque-user-token':
            rejections.append(request)
            if len(rejections) == 2:
                both_rejected.set()
            await both_rejected.wait()
            return httpx.Response(401, json={})
        assert request.headers['Authorization'] == 'Bearer rotated-access'
        return httpx.Response(200, json={'installations': [{'id': 10}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        env.service.client = client
        results = await asyncio.wait_for(asyncio.gather(*(
            env.service.user_request_pages(env.user.id, '/user/installations', 'installations')
            for _ in range(2))), timeout=5)
    assert results == [[{'id': 10}], [{'id': 10}]]
    assert len(refreshes) == 1


@pytest.mark.asyncio
async def test_user_authorization_rejected_after_refresh_requires_reconnect(env):
    credentials(env, seconds=3600)
    async def transport(request):
        if request.url.host == 'github.com':
            return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                'expires_in': 28800, 'refresh_token_expires_in': 15897600})
        return httpx.Response(401, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        env.service.client = client
        with pytest.raises(GitHubAppError) as exc:
            await env.service.user_request_pages(env.user.id, '/user/installations', 'installations')
    assert exc.value.reconnect_required
    assert env.store.get_user_github_connection(env.user.id).access_status == 'RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_rate_limited_discovery_keeps_user_authorization(env):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(403, headers={'X-RateLimit-Remaining': '0'}, json={}))) as client:
        env.service.client = client
        with pytest.raises(GitHubAppError) as exc:
            await env.service.discovery(env.user.id)
    assert exc.value.status_code == 503 and not exc.value.reconnect_required
    assert env.store.get_user_github_connection(env.user.id).access_status == 'ACTIVE'


@pytest.mark.asyncio
async def test_indexing_credential_ignores_expired_user_and_refresh_tokens(env):
    tracked = await track(env)
    credentials(env, refresh_seconds=-1)
    env.service.user_token = AsyncMock(side_effect=AssertionError('Indexing must not use user OAuth'))
    credential = await env.service.indexing_credential(tracked.id, '10')
    assert credential.token.startswith('opaque-installation-token')
    env.service.user_token.assert_not_awaited()


def test_schema_one_requires_explicit_refresh_upgrade(env, monkeypatch):
    from ai_services.ingestion.persistence import database
    with env.engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
        conn.exec_driver_sql('INSERT INTO app_schema_version VALUES (1)')
    monkeypatch.setattr(database, 'get_engine', lambda: env.engine)
    with pytest.raises(RuntimeError, match='migrate_github_refresh'):
        database.init_db()
    upgrade(env.engine)
    database.init_db()


def test_new_database_starts_at_schema_two(tmp_path, monkeypatch):
    from ai_services.ingestion.persistence import database
    engine = create_engine(f'sqlite:///{tmp_path / "new.db"}')
    monkeypatch.setattr(database, 'get_engine', lambda: engine)
    database.init_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() == 2
        assert 'refresh_token' in {c['name'] for c in inspect(conn).get_columns('github_connections')}
    engine.dispose()


def test_delayed_revocation_probe_does_not_revoke_new_login(env):
    async def delayed_denial(method, path, token):
        env.store.upsert_github_login('1', 'alice', access_token='reauthorized-access',
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8), refresh_token='reauthorized-refresh',
            refresh_expires_at=datetime.now(timezone.utc) + timedelta(days=180))
        raise GitHubAppError(401, 'Old credential rejected')
    env.service.request = delayed_denial
    response = webhook(env, 'github_app_authorization', {'action': 'revoked', 'sender': {'id': 1}})
    assert response.status_code == 200
    connection = env.store.get_user_github_connection(env.user.id)
    assert connection.access_status == 'ACTIVE'
    assert env.store.decrypt_token(connection.access_token) == 'reauthorized-access'
