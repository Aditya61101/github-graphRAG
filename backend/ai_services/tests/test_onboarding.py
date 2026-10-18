from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect

from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence import database
from ai_services.ingestion.persistence.migrate_onboarding import upgrade
from ai_services.tests.test_github_app import (env, begin_login, callback, installation, repo,
    start_installation, auth_result, finish_installation_callback, expire_auth_state)
from ai_services.tests.test_github_refresh import credentials
from api_services.app.utils.jwt_utils import create_access_token


def start(env):
    return start_installation(env)


def returned(response):
    return auth_result(response)


def assert_incomplete(env):
    assert env.store.get_user(env.user.id).onboarding_completed_at is None
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()
    assert env.store.list_repositories(env.user.id) == []


def legacy_database(tmp_path, with_column=False, version=2):
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
        conn.exec_driver_sql(f'INSERT INTO app_schema_version VALUES ({version})')
        extra = ', onboarding_completed_at DATETIME' if with_column else ''
        conn.exec_driver_sql('CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT' + extra + ')')
        conn.exec_driver_sql("INSERT INTO users (id, username) VALUES ('legacy', 'alice')")
        conn.exec_driver_sql('CREATE TABLE github_connections (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), access_token TEXT, refresh_token TEXT)')
        conn.exec_driver_sql("INSERT INTO github_connections VALUES ('conn', 'legacy', 'encrypted-access', 'encrypted-refresh')")
        conn.exec_driver_sql('CREATE TABLE repositories (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id))')
        conn.exec_driver_sql("INSERT INTO repositories VALUES ('repo', 'legacy')")
        conn.exec_driver_sql('CREATE TABLE adrs (id TEXT PRIMARY KEY, repository_id TEXT REFERENCES repositories(id))')
        conn.exec_driver_sql("INSERT INTO adrs VALUES ('adr', 'repo')")
    return engine


def test_migration_backfills_only_existing_users_and_preserves_records(tmp_path):
    engine = legacy_database(tmp_path)
    try:
        upgrade(engine)
        with engine.begin() as conn:
            first = conn.exec_driver_sql("SELECT onboarding_completed_at FROM users WHERE id='legacy'").scalar()
            assert first
            conn.exec_driver_sql("INSERT INTO users (id, username) VALUES ('new', 'bob')")
        upgrade(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() == 3
            assert conn.exec_driver_sql("SELECT onboarding_completed_at FROM users WHERE id='legacy'").scalar() == first
            assert conn.exec_driver_sql("SELECT onboarding_completed_at FROM users WHERE id='new'").scalar() is None
            assert conn.exec_driver_sql('SELECT * FROM github_connections').one() == ('conn', 'legacy', 'encrypted-access', 'encrypted-refresh')
            assert conn.exec_driver_sql('SELECT * FROM repositories').one() == ('repo', 'legacy')
            assert conn.exec_driver_sql('SELECT * FROM adrs').one() == ('adr', 'repo')
            assert inspect(conn).get_foreign_keys('adrs')[0]['referred_table'] == 'repositories'
            column = next(c for c in inspect(conn).get_columns('users') if c['name'] == 'onboarding_completed_at')
            assert column['nullable'] and column['default'] is None
    finally:
        engine.dispose()


def test_migration_preserves_an_existing_completion_timestamp(tmp_path):
    engine = legacy_database(tmp_path, with_column=True)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET onboarding_completed_at='2020-01-01 00:00:00'")
        upgrade(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT onboarding_completed_at FROM users').scalar() == '2020-01-01 00:00:00'
    finally:
        engine.dispose()


def test_migration_rolls_back_schema_backfill_and_version_on_failure(tmp_path):
    engine = legacy_database(tmp_path)
    def fail_version_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE app_schema_version'):
            raise RuntimeError('simulated interruption')
    event.listen(engine, 'before_cursor_execute', fail_version_update)
    try:
        with pytest.raises(RuntimeError, match='simulated interruption'):
            upgrade(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() == 2
            assert 'onboarding_completed_at' not in {c['name'] for c in inspect(conn).get_columns('users')}
        event.remove(engine, 'before_cursor_execute', fail_version_update)
        upgrade(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize('version', [1, 4])
def test_migration_rejects_unsupported_versions_without_changes(tmp_path, version):
    engine = legacy_database(tmp_path, version=version)
    try:
        with pytest.raises(RuntimeError, match='Unsupported'):
            upgrade(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() == version
            assert 'onboarding_completed_at' not in {c['name'] for c in inspect(conn).get_columns('users')}
    finally:
        engine.dispose()


def test_migration_rejects_malformed_v3_without_backfilling(tmp_path):
    engine = legacy_database(tmp_path, version=3)
    try:
        with pytest.raises(RuntimeError, match='missing'):
            upgrade(engine)
    finally:
        engine.dispose()


def test_v2_startup_requires_explicit_upgrade_and_legacy_login_is_completed(env, monkeypatch):
    with env.engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
        conn.exec_driver_sql('INSERT INTO app_schema_version VALUES (2)')
    monkeypatch.setattr(database, 'get_engine', lambda: env.engine)
    with pytest.raises(RuntimeError, match='migrate_onboarding'):
        database.init_db()
    upgrade(env.engine)
    database.init_db()
    env.state['user_installations'] = []
    result = returned(callback(env, begin_login(env)['state'][0]))
    assert result['next'] == '/projects'
    assert env.client.get('/auth/me', headers=env.headers).json()['onboarding_completed']
    new, _ = env.store.upsert_github_login('2', 'bob', access_token='bob-token',
                                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert new.onboarding_completed_at is None
    upgrade(env.engine)
    assert env.store.get_user(new.id).onboarding_completed_at is None


def test_new_login_with_existing_grants_still_requires_onboarding(env):
    result = returned(callback(env, begin_login(env)['state'][0]))
    assert result['next'] == '/install'
    assert_incomplete(env)
    response = env.client.get('/auth/onboarding', headers=env.headers).json()
    assert response['screen'] == 'available_repositories' and response['eligible']


def test_me_is_lightweight_even_during_github_failure(env):
    env.state['user_status'] = 503
    env.service.discovery = AsyncMock(side_effect=AssertionError('must not discover'))
    response = env.client.get('/auth/me', headers=env.headers)
    assert response.status_code == 200
    body = response.json()
    assert body['installations'] == [] and body['installation_discovery_status'] == 'not_requested'
    assert body['next'] == '/install'
    assert env.state['calls'] == []
    env.service.discovery.assert_not_awaited()


@pytest.mark.parametrize('endpoint,method', [('/auth/me', 'get'), ('/auth/onboarding', 'get'),
    ('/auth/onboarding/complete', 'post'), ('/auth/onboarding/installation/verify', 'post')])
def test_onboarding_endpoints_require_authentication(env, endpoint, method):
    assert getattr(env.client, method)(endpoint).status_code == 401


def test_completion_is_idempotent_and_survives_grant_removal_and_relogin(env):
    first = env.client.post('/auth/onboarding/complete', headers=env.headers)
    assert first.status_code == 200 and first.json()['next'] == '/projects'
    timestamp = first.json()['onboarding_completed_at']
    assert timestamp
    env.state['user_installations'] = []
    env.state['user_status'] = 503
    second = env.client.post('/auth/onboarding/complete', headers=env.headers)
    assert second.json() == first.json()
    assert env.client.get('/auth/me', headers=env.headers).json()['onboarding_completed_at'] == timestamp
    env.state['user_status'] = 200
    assert returned(callback(env, begin_login(env)['state'][0]))['next'] == '/projects'
    assert env.store.get_user(env.user.id).to_dict()['onboarding_completed_at'] == timestamp
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


@pytest.mark.parametrize('case', ['no_installation', 'private_only', 'inventory_only', 'suspended'])
def test_first_completion_rejects_ineligible_access(env, case):
    if case == 'no_installation':
        env.state['user_installations'] = []
    elif case == 'private_only':
        env.state['inventory'][10] = env.state['user_repositories'][10] = [repo(private=True)]
    elif case == 'inventory_only':
        env.state['user_repositories'][10] = []
    else:
        env.state['installations'][10]['suspended_at'] = '2026-01-01T00:00:00Z'
    response = env.client.post('/auth/onboarding/complete', headers=env.headers)
    assert response.status_code == 409
    assert response.json()['detail']['outcome'] == 'no_eligible_repositories'
    assert_incomplete(env)


@pytest.mark.parametrize('endpoint,method', [('/auth/onboarding', 'get'), ('/auth/onboarding/complete', 'post')])
def test_temporary_discovery_failure_never_becomes_empty_or_completed(env, endpoint, method):
    env.state['user_status'] = 503
    response = getattr(env.client, method)(endpoint, headers=env.headers)
    assert response.status_code == 503
    assert response.json()['detail']['outcome'] == 'temporary_failure'
    assert 'installations' not in response.json()
    assert_incomplete(env)


def test_multiple_installations_group_only_user_accessible_public_repositories(env):
    env.state['inventory'][10] = [repo(101), repo(102), repo(103, private=True)]
    env.state['user_repositories'][10] = [repo(101), repo(103, private=True), repo(999)]
    second = installation(20, account={'id': 20, 'login': 'other-org', 'type': 'Organization'})
    env.state['installations'][20] = second
    env.state['user_installations'].append(second)
    env.state['inventory'][20] = [repo(201), repo(202)]
    env.state['user_repositories'][20] = [repo(202)]
    response = env.client.get('/auth/onboarding', headers=env.headers)
    assert response.status_code == 200
    groups = response.json()['installations']
    assert [g['id'] for g in groups] == ['10', '20']
    assert [[r['github_repository_id'] for r in g['repositories']] for g in groups] == [['101'], ['202']]
    assert [g['public_repository_count'] for g in groups] == [1, 1]
    assert_incomplete(env)


def test_two_collaborators_never_inherit_cached_installation_inventory(env):
    env.state['inventory'][10] = [repo(101), repo(102)]
    env.state['user_repositories'][10] = [repo(101)]
    assert env.client.get('/auth/onboarding', headers=env.headers).json()['installations'][0]['public_repository_count'] == 1
    bob, _ = env.store.upsert_github_login('2', 'bob', access_token='bob-token',
                                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    headers = {'Authorization': 'Bearer ' + create_access_token({'sub': bob.id})}
    original = env.service.client
    bob_repositories = [repo(102)]
    async def transport(request):
        if (request.url.path == '/user/installations/10/repositories' and
                request.headers['Authorization'] == 'Bearer bob-token'):
            return httpx.Response(200, json={'repositories': bob_repositories})
        return await original.send(request)
    env.service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    response = env.client.get('/auth/onboarding', headers=headers).json()
    assert [r['github_repository_id'] for r in response['installations'][0]['repositories']] == ['102']
    alice = env.client.get('/auth/onboarding', headers=env.headers).json()
    assert [r['github_repository_id'] for r in alice['installations'][0]['repositories']] == ['101']
    bob_repositories.clear()
    assert env.client.post('/auth/onboarding/complete', headers=headers).status_code == 409
    assert env.store.get_user(bob.id).onboarding_completed_at is None


@pytest.mark.parametrize('action,with_code', [('install', True), ('update', True), ('update', False)])
def test_successful_installation_and_configuration_complete_directly(env, action, with_code):
    state = start(env)
    params = {'state': state, 'installation_id': '10', 'setup_action': action}
    if with_code:
        params['code'] = 'code'
    result = returned(finish_installation_callback(env, params))
    assert result['installation_outcome'] == 'success' and result['next'] == '/projects'
    assert env.store.get_user(env.user.id).onboarding_completed_at
    assert env.store.list_repositories(env.user.id) == []
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


@pytest.mark.parametrize('params,outcome', [
    ({'error': 'access_denied', 'installation_id': '10', 'setup_action': 'install'}, 'cancelled'),
    ({}, 'cancelled'),
    ({'installation_id': '10'}, 'cancelled'),
    ({'installation_id': '20', 'setup_action': 'request'}, 'pending_approval'),
    ({'error': 'temporarily_unavailable', 'installation_id': '10', 'setup_action': 'install'}, 'temporary_failure'),
])
def test_cancelled_pending_or_incomplete_attempt_cannot_use_existing_grants(env, params, outcome):
    params = {'state': start(env), **params}
    result = returned(finish_installation_callback(env, params))
    assert result['installation_outcome'] == outcome and result['next'] == '/install'
    assert env.state['calls'] == []
    env.oauth.client.fetch_access_token.assert_not_awaited()
    assert env.client.post('/auth/onboarding/installation/verify', headers=env.headers).status_code == 409
    assert_incomplete(env)


def test_new_installation_without_eligible_repositories_cannot_use_other_installation(env):
    second = installation(20)
    env.state['installations'][20] = second
    env.state['user_installations'].append(second)
    env.state['inventory'][20] = env.state['user_repositories'][20] = [repo(201, private=True)]
    result = returned(callback(env, start(env), installation_id='20', setup_action='install'))
    assert result['installation_outcome'] == 'no_eligible_repositories'
    assert_incomplete(env)


def test_transient_return_can_be_verified_again_without_replaying_oauth(env):
    state = start(env)
    # Identity authorization must succeed before a retryable verified return
    # exists; simulate failure only in installation discovery afterwards.
    discovery = env.service.discovery
    env.service.discovery = AsyncMock(side_effect=GitHubAppError(503, 'Temporary discovery failure'))
    result = returned(finish_installation_callback(env, {
        'state': state, 'installation_id': '10', 'setup_action': 'update'}))
    assert result['installation_outcome'] == 'temporary_failure'
    assert_incomplete(env)
    env.service.discovery = discovery
    result = env.client.post('/auth/onboarding/installation/verify', headers=env.headers)
    assert result.status_code == 200 and result.json()['installation_outcome'] == 'success'
    assert result.json()['next'] == '/projects'
    assert env.client.get('/auth/github/callback', params={'state': state, 'installation_id': '10'}).status_code == 400


def test_retry_is_user_bound_expires_and_cannot_be_supplied_by_client(env):
    returned(callback(env, start(env), installation_id='10', setup_action='update'))
    with TestClient(env.app, follow_redirects=False) as other:
        # Bearer-authenticated same-user retry does not require third-party cookies.
        assert other.post('/auth/onboarding/installation/verify', headers=env.headers).status_code == 200
    expire_auth_state(env, 'installation_return', env.user.id)
    assert env.client.post('/auth/onboarding/installation/verify', headers=env.headers,
                           json={'installation_id': '10', 'outcome': 'success'}).status_code == 409


def test_cancellation_after_completion_keeps_persisted_completion(env):
    first = env.store.complete_onboarding(env.user.id).onboarding_completed_at
    result = returned(finish_installation_callback(env, {
        'state': start(env), 'error': 'access_denied'}))
    assert result['installation_outcome'] == 'cancelled' and result['next'] == '/projects'
    assert env.store.get_user(env.user.id).onboarding_completed_at == first


def test_code_less_configuration_return_reauthorizes_current_github_account(env):
    credentials(env)
    original = env.service.client
    refreshes = []
    async def transport(request):
        if request.url.host == 'github.com':
            refreshes.append(request)
            return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                'expires_in': 28800, 'refresh_token_expires_in': 15897600})
        if request.url.path.startswith('/user/'):
            assert request.headers['Authorization'] == 'Bearer opaque-user-token'
        return await original.send(request)
    env.service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    result = returned(finish_installation_callback(env, {
        'state': start(env), 'installation_id': '10', 'setup_action': 'update'}))
    assert result['installation_outcome'] == 'success' and len(refreshes) == 0
    connection = env.store.get_user_github_connection(env.user.id)
    assert env.store.decrypt_token(connection.refresh_token) == 'login-refresh-token'


def test_reconnect_does_not_reset_completion_or_destroy_app_auth(env):
    env.store.complete_onboarding(env.user.id)
    env.service.revoke_user('1')
    status = env.client.get('/auth/me', headers=env.headers)
    assert status.status_code == 200 and status.json()['next'] == '/projects'
    assert status.json()['reconnect_required'] and status.json()['onboarding_completed']
    response = env.client.get('/auth/onboarding', headers=env.headers)
    assert response.status_code == 409
    assert response.json()['detail']['outcome'] == 'reauthorization_required'
    assert env.client.post('/auth/onboarding/complete', headers=env.headers).status_code == 200


def test_return_verification_auth_failure_is_distinct_from_no_grants(env):
    env.service.discovery = AsyncMock(side_effect=GitHubAppError(401, 'reconnect', reconnect_required=True))
    result = returned(finish_installation_callback(env, {
        'state': start(env), 'installation_id': '10', 'setup_action': 'update'}))
    assert result['installation_outcome'] == 'reauthorization_required'
    assert_incomplete(env)


def test_expired_and_wrong_browser_installation_states_are_rejected(env):
    state = start(env)
    with TestClient(env.app, follow_redirects=False) as other:
        assert other.get('/auth/github/callback', params={
            'state': state, 'installation_id': '10', 'setup_action': 'update'}).status_code == 400
    state = start(env)
    expire_auth_state(env, 'oauth', state)
    assert env.client.get('/auth/github/callback', params={
        'state': state, 'installation_id': '10', 'setup_action': 'update'}).status_code == 400
    assert_incomplete(env)
