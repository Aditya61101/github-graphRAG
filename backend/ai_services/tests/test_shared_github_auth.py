"""Shared-state and cross-site contracts; no live GitHub or production databases."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from ai_services.github_app.auth_state import AuthStateStore, InvalidAuthState
from ai_services.github_app.service import GitHubAppService, GitHubAppError
from ai_services.ingestion.persistence.models import Base, GitHubAuthState, GitHubConnectionModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.tests.test_github_app import env, auth_result, expire_auth_state
from ai_services.tests.test_github_refresh import credentials, refreshing
from api_services.app.routers.auth import router, GitHubOAuth
from api_services.app.utils.auth_logging import AuthQueryRedactionFilter

API = 'https://api.example.test'
WEB = 'https://website.other.test'


def instance(env, store=None):
    settings = replace(env.settings, callback_url=API + '/auth/github/callback', frontend_url=WEB)
    store = store or env.store
    service = GitHubAppService(store, settings, env.service.client)
    flow = GitHubOAuth(settings, store.session_factory)
    flow.client.fetch_access_token = env.oauth.client.fetch_access_token
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=600,
                       same_site='lax', https_only=True)
    app.include_router(router, prefix='/auth')
    app.state.github_oauth, app.state.github_app = flow, service
    app.state.sqlite_store = store
    return SimpleNamespace(client=TestClient(app, base_url=API, follow_redirects=False), flow=flow, service=service)


def begin(client):
    response = client.get('/auth/github/login')
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers['location']).query)['state'][0]


def install_handoff(client, headers):
    response = client.post('/auth/github/install', headers={**headers,
        'X-DecisionGuard-Auth-Flow': 'post-handoff-v1', 'Origin': WEB})
    assert response.status_code == 200
    assert 'set-cookie' not in response.headers
    contract = response.json()
    assert not urlsplit(contract['action']).query
    return contract


def continue_install(client, contract, origin=WEB):
    return client.post('/auth/github/install/continue', headers={'Origin': origin}, data=contract['fields'])


def test_cross_site_handoff_needs_no_third_party_cookie_and_callback_can_change_instance(env):
    a, b, c = instance(env), instance(env), instance(env)
    try:
        contract = install_handoff(a.client, env.headers)
        assert not a.client.cookies and not b.client.cookies
        continuation = continue_install(b.client, contract)
        assert continuation.status_code == 303
        cookie = continuation.headers['set-cookie'].lower()
        assert 'samesite=lax' in cookie and 'secure' in cookie and 'httponly' in cookie
        state = parse_qs(urlsplit(continuation.headers['location']).query)['state'][0]
        c.client.cookies.update(b.client.cookies)
        response = c.client.get('/auth/github/callback', params={
            'state': state, 'installation_id': '10', 'setup_action': 'install', 'code': 'synthetic-code'})
        result = auth_result(response)
        assert result['installation_outcome'] == 'success' and result['next'] == '/projects'
        assert env.store.get_user(env.user.id).onboarding_completed_at
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        assert 'frame-ancestors' in response.headers['Content-Security-Policy']
        assert 'location' not in response.headers and '?token=' not in response.text
        assert 'postMessage(result, "' + WEB + '")' in response.text
        assert continue_install(a.client, contract).status_code == 400
    finally:
        for value in (a, b, c):
            value.client.close()


def test_login_state_and_pkce_survive_instance_restart_and_missing_cookie_is_rejected(env):
    a, b = instance(env), instance(env)
    try:
        state = begin(a.client)
        assert b.client.get('/auth/github/callback', params={'state': state, 'code': 'code'}).status_code == 400
        env.oauth.client.fetch_access_token.assert_not_awaited()
        b.client.cookies.update(a.client.cookies)
        response = b.client.get('/auth/github/callback', params={'state': state, 'code': 'code'})
        assert auth_result(response)['next'] == '/install'
        assert env.oauth.client.fetch_access_token.await_args.kwargs['code_verifier']
        assert b.client.get('/auth/github/callback', params={'state': state, 'code': 'code'}).status_code == 400
    finally:
        a.client.close()
        b.client.close()


@pytest.mark.parametrize('case', ['expired', 'tampered', 'missing'])
def test_bad_oauth_state_never_exchanges_code(env, case):
    a = instance(env)
    try:
        state = begin(a.client)
        if case == 'expired':
            with env.store.session_factory.begin() as s:
                s.get(GitHubAuthState, a.flow.states.key('oauth', state)).expires_at = 0
        elif case == 'tampered':
            state += 'x'
        else:
            state = ''
        response = a.client.get('/auth/github/callback', params={'state': state})
        assert response.status_code == 400
        env.oauth.client.fetch_access_token.assert_not_awaited()
    finally:
        a.client.close()


def test_handoff_origin_proof_expiry_and_legacy_client_are_checked(env):
    a = instance(env)
    try:
        assert a.client.post('/auth/github/install', headers={'X-DecisionGuard-Auth-Flow': 'post-handoff-v1'}).status_code == 401
        assert a.client.post('/auth/github/install', headers=env.headers).status_code == 409
        contract = install_handoff(a.client, env.headers)
        assert continue_install(a.client, contract, 'https://attacker.test').status_code == 403
        assert continue_install(a.client, {'fields': {'handoff': 'unknown' * 8}}).status_code == 400
        assert continue_install(a.client, contract).status_code == 303  # wrong origin did not burn it
        contract = install_handoff(a.client, env.headers)
        with env.store.session_factory.begin() as s:
            s.get(GitHubAuthState, a.flow.states.key('handoff', contract['fields']['handoff'])).expires_at = 0
        assert continue_install(a.client, contract).status_code == 400
    finally:
        a.client.close()


def test_new_attempt_invalidates_older_callback_and_changed_account_cannot_continue(env):
    a = instance(env)
    try:
        old = install_handoff(a.client, env.headers)
        response = continue_install(a.client, old)
        state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        newest = install_handoff(a.client, env.headers)
        assert a.client.get('/auth/github/callback', params={
            'state': state, 'code': 'code', 'installation_id': '10', 'setup_action': 'install'}).status_code == 400
        with env.store.session_factory.begin() as s:
            s.get(GitHubConnectionModel, env.connection.id).github_user_id = '2'
        assert continue_install(a.client, newest).status_code == 403
        env.oauth.client.fetch_access_token.assert_not_awaited()
    finally:
        a.client.close()


def test_concurrent_callbacks_atomically_consume_state_once(env, tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "auth-concurrency.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    store = SqliteApplicationStore(sessionmaker(engine, expire_on_commit=False))
    a, b, c = instance(env, store), instance(env, store), instance(env, store)
    try:
        state = begin(a.client)
        b.client.cookies.update(a.client.cookies)
        c.client.cookies.update(a.client.cookies)
        def invoke(client):
            return client.get('/auth/github/callback', params={'state': state, 'code': 'code'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(invoke, [b.client, c.client]))
        assert sorted(statuses) == [200, 400]
        assert env.oauth.client.fetch_access_token.await_count == 1
    finally:
        for value in (a, b, c):
            value.client.close()
        engine.dispose()


def test_shared_state_contains_no_plaintext_lookup_or_pkce_secrets_and_expires(env):
    a = instance(env)
    try:
        state = begin(a.client)
        with env.store.session_factory() as s:
            row = s.get(GitHubAuthState, a.flow.states.key('oauth', state))
            verifier = a.flow.states.decode(row)['oauth']['code_verifier']
            assert state not in row.key and verifier not in row.payload and 'code_verifier' not in row.payload
        # Cleanup is bounded and lazy; records expired for security regardless of physical deletion.
        with env.store.session_factory.begin() as s:
            s.get(GitHubAuthState, a.flow.states.key('oauth', state)).expires_at = 0
        begin(a.client)
        with env.store.session_factory() as s:
            assert s.get(GitHubAuthState, a.flow.states.key('oauth', state)) is None
    finally:
        a.client.close()


async def test_two_instances_do_not_rotate_same_refresh_token_concurrently(env):
    credentials(env)
    client, calls = refreshing(env)
    other = GitHubAppService(env.store, env.settings, client)
    async with client:
        results = await asyncio.gather(env.service.user_token(env.user.id), other.user_token(env.user.id), return_exceptions=True)
        assert 'rotated-access' in results
        assert sum(isinstance(x, GitHubAppError) and x.status_code == 503 for x in results) == 1
        assert await other.user_token(env.user.id) == 'rotated-access'
    assert len(calls) == 1


async def test_pending_rotation_can_finish_on_another_instance(env, monkeypatch):
    credentials(env)
    persist = env.store.rotate_github_tokens
    monkeypatch.setattr(env.store, 'rotate_github_tokens', Mock(side_effect=RuntimeError('private storage failure')))
    client, calls = refreshing(env)
    async with client:
        with pytest.raises(GitHubAppError):
            await env.service.user_token(env.user.id)
        monkeypatch.setattr(env.store, 'rotate_github_tokens', persist)
        other = GitHubAppService(env.store, env.settings, client)
        assert not other.pending_rotations
        assert await other.user_token(env.user.id) == 'rotated-access'
    assert len(calls) == 1


def test_access_logs_redact_auth_query_without_changing_other_requests():
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1, '%s %s %s %s %s',
        ('client', 'GET', '/auth/github/callback?code=secret-code&state=secret-state', '1.1', 200), None)
    assert AuthQueryRedactionFilter().filter(record)
    assert record.args[2] == '/auth/github/callback'
    assert 'secret' not in record.getMessage()


def test_new_login_renews_browser_cookie_and_canonicalizes_api_alias(env):
    a = instance(env)
    try:
        first = a.client.get('/auth/github/login')
        second = a.client.get('/auth/github/login')
        assert 'Max-Age=600' in first.headers['set-cookie']
        assert 'Max-Age=600' in second.headers['set-cookie']
        response = a.client.get('https://alias.example.test/auth/github/login?state=untrusted')
        assert response.status_code == 303
        assert response.headers['location'] == API + '/auth/github/login'
        assert 'set-cookie' not in response.headers
    finally:
        a.client.close()


def test_expired_credential_owner_cannot_overwrite_or_release_new_owner(env):
    states = env.service.auth_states
    old = states.acquire_credentials(env.user.id)
    with env.store.session_factory.begin() as s:
        s.get(GitHubAuthState, states.key('credential_lock', env.user.id)).expires_at = 0
    new = states.acquire_credentials(env.user.id)
    previous = env.store.get_user_github_connection(env.user.id)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(InvalidAuthState):
        env.store.rotate_github_tokens(previous, 'stale-access', 'stale-refresh', expiry, expiry, old)
    assert env.store.get_user_github_connection(env.user.id).access_token == previous.access_token
    states.release_credentials(env.user.id, old)
    with pytest.raises(InvalidAuthState):
        states.acquire_credentials(env.user.id)
    states.release_credentials(env.user.id, new)


def test_existing_user_lease_cannot_create_identity_after_account_binding_changes(env):
    states = env.service.auth_states
    owner = states.acquire_credentials(env.user.id)
    try:
        with env.store.session_factory.begin() as s:
            s.get(GitHubConnectionModel, env.connection.id).github_user_id = '2'
        with pytest.raises(InvalidAuthState):
            env.store.upsert_github_login('1', 'alice', access_token='unused', lease_owner=owner)
        assert env.store.get_user_github_connection(env.user.id).github_user_id == '2'
    finally:
        states.release_credentials(env.user.id, owner)


def test_completion_uses_current_connection_not_an_older_account_row(env):
    states = env.oauth.states
    _, attempt = states.start_installation(env.user.id, '1')
    with env.store.session_factory.begin() as s:
        s.add(GitHubConnectionModel(id='newer-connection', user_id=env.user.id,
            github_user_id='2', access_token='unused', updated_at=datetime.now(timezone.utc) + timedelta(seconds=10)))
    with pytest.raises(InvalidAuthState):
        states.complete_installation(env.user.id, '1', attempt)
    assert env.store.get_user(env.user.id).onboarding_completed_at is None
