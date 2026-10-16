from datetime import UTC, datetime, timedelta

import pytest

from ai_services.ingestion.persistence.models import RepositoryModel
from ai_services.tests.test_github_app import env as env  # noqa: PLC0414 - re-export the pytest fixture
from ai_services.tests.test_github_app import installation, repo
from ai_services.tests.test_github_refresh import credentials


def permissions(env, admin):
    item = env.state['user_repositories'][10][0]
    if admin is None:
        item.pop('permissions', None)
    else:
        item['permissions'] = {'admin': admin, 'push': True, 'pull': True}


def owned(env, status='IDLE'):
    env.service.persist_inventory(env.state['installations'][10], env.state['inventory'][10], 'ACTIVE')
    selected = dict(env.state['user_repositories'][10][0], installation_id='10')
    record = env.service.bind_repository(selected, env.user.id)
    with env.store.session_factory() as session:
        session.get(RepositoryModel, record.id).status = status
        session.commit()
    return record


@pytest.mark.parametrize('admin,allowed,reason', [
    (True, True, 'allowed'), (False, False, 'github_admin_required'),
    (None, None, 'permission_unknown'), ('true', None, 'permission_unknown'),
    (1, None, 'permission_unknown'),
])
def test_user_permission_capabilities_are_explicit_and_tristate(env, admin, allowed, reason):
    permissions(env, admin)
    response = env.client.get('/repositories', headers=env.headers)
    assert response.status_code == 200
    item = response.json()[0]
    assert item['tracked_repository_id'] is None and item['status'] == 'NOT_INDEXED'
    assert item['monitoring']['can_enable_monitoring'] is allowed
    assert item['monitoring']['reason'] == reason
    assert item['monitoring']['github_admin'] is (admin if type(admin) is bool else None)
    assert item['monitoring']['can_retry_indexing'] is False
    assert env.store.list_repositories(env.user.id) == []
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


def test_user_permissions_not_inventory_or_owner_name_authorize_activation(env):
    env.state['inventory'][10][0]['permissions']['admin'] = False
    permissions(env, True)
    response = env.client.post('/repositories/ingest', headers=env.headers,
                               json={'repository': 'owner/repo101', 'installation_id': '10'})
    assert response.status_code == 202
    assert env.store.get_repository('101').user_id == env.user.id
    # Alice can administer an organization repo whose owner name is different.
    assert env.user.username != 'owner'
    env.app.state.ingestion_service.ingest_repository.assert_awaited_once()


@pytest.mark.parametrize('admin,status', [(False, 403), (None, 503), ('true', 503)])
def test_direct_and_forged_ingest_requests_recheck_admin_before_registration(env, admin, status):
    permissions(env, admin)
    response = env.client.post('/repositories/ingest', headers=env.headers, json={
        'repository': 'owner/repo101', 'installation_id': '10',
        'github_admin': True, 'can_enable_monitoring': True,
    })
    assert response.status_code == status
    assert env.store.list_repositories() == []
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


def test_admin_revoked_after_discovery_cannot_activate(env):
    assert env.client.get('/repositories', headers=env.headers).json()[0]['monitoring']['can_enable_monitoring']
    permissions(env, False)
    assert env.client.post('/repositories/ingest', headers=env.headers,
                           json={'repository': 'owner/repo101'}).status_code == 403
    assert env.store.list_repositories() == []
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


def test_missing_permissions_are_not_satisfied_by_inventory_admin(env):
    permissions(env, None)
    assert env.state['inventory'][10][0]['permissions']['admin'] is True
    assert env.client.post('/repositories/ingest', headers=env.headers,
                           json={'repository': 'owner/repo101'}).status_code == 503


def test_owned_failed_repository_needs_live_admin_for_retry_but_not_read(env):
    record = owned(env, 'FAILED')
    capabilities = env.client.get('/repositories', headers=env.headers).json()[0]['monitoring']
    assert capabilities['can_retry_indexing'] is True
    permissions(env, False)
    assert env.client.post('/repositories/owner/repo101/sync', headers=env.headers,
                           json={'sync': False}).status_code == 403
    assert env.client.get('/repositories/owner/repo101', headers=env.headers).status_code == 200
    assert env.store.get_repository(record.id).status == 'FAILED'
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()
    permissions(env, True)
    assert env.client.post('/repositories/owner/repo101/sync', headers=env.headers,
                           json={'sync': False}).status_code == 202
    env.app.state.ingestion_service.ingest_repository.assert_awaited_once()


def test_other_decisionguard_owner_is_restricted_even_for_github_admin(env):
    other, _ = env.store.upsert_github_login('2', 'bob', access_token='bob-token',
        expires_at=datetime.now(UTC) + timedelta(hours=1))
    selected = dict(env.state['user_repositories'][10][0], installation_id='10')
    env.service.persist_inventory(env.state['installations'][10], env.state['inventory'][10], 'ACTIVE')
    record = env.service.bind_repository(selected, other.id)
    response = env.client.get('/repositories', headers=env.headers).json()[0]
    assert response['monitoring']['github_admin'] is True
    assert response['monitoring']['can_enable_monitoring'] is False
    assert response['monitoring']['reason'] == 'owned_by_another_user'
    assert response['tracked_repository_id'] is None and response['indexed_commit_sha'] is None
    assert env.client.post('/repositories/ingest', headers=env.headers,
                           json={'repository': 'owner/repo101'}).status_code == 409
    assert env.client.post('/repositories/owner/repo101/sync', headers=env.headers).status_code == 403
    assert env.store.get_repository(record.id).user_id == other.id
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()


def test_multiple_installations_prefer_owned_binding_and_never_duplicate_cards(env):
    record = owned(env)
    env.state['installations'][20] = installation(20)
    env.state['inventory'][20] = [repo()]
    env.state['user_installations'] = [installation(20), installation(10)]
    env.state['user_repositories'][20] = [repo()]
    response = env.client.get('/repositories', headers=env.headers).json()
    assert len(response) == 1 and response[0]['installation_id'] == record.installation_id
    assert response[0]['monitoring']['can_enable_monitoring'] is True


def test_activation_preserves_automatic_refresh_and_installed_public_rule(env):
    import httpx
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
    response = env.client.post('/repositories/ingest', headers=env.headers,
                               json={'repository': 'owner/repo101'})
    assert response.status_code == 202 and len(refreshes) == 1
    assert 'rotated-' not in response.text
    assert env.store.get_user_github_connection(env.user.id).access_status == 'ACTIVE'


def test_public_visibility_and_installation_removal_still_block_admin_activation(env):
    env.state['inventory'][10] = [repo(private=True)]
    assert env.client.get('/repositories', headers=env.headers).json() == []
    assert env.client.post('/repositories/ingest', headers=env.headers,
                           json={'repository': 'owner/repo101'}).status_code == 403
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()
