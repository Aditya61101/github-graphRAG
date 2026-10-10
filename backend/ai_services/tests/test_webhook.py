"""Dispatch regressions that do not require a clone or external services."""
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ai_services.github_app.service import GitHubAppError
from api_services.app.routers.webhook import router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router, prefix='/webhooks')
    tracked = SimpleNamespace(id='repo_101', installation_id='10', full_name='owner/repo', tracked_branch='main')
    app.state.github_app = SimpleNamespace(
        settings=SimpleNamespace(webhook_secret='test-secret'),
        store=SimpleNamespace(get_repository=Mock(return_value=tracked)),
        invalidate=Mock(), reconcile=AsyncMock(), indexing_credential=AsyncMock())
    app.state.ingestion_service = SimpleNamespace(handle_github_push_webhook=AsyncMock())
    return app


def send(app, event, payload):
    raw = json.dumps(payload).encode()
    signature = hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        return client.post('/webhooks/github', content=raw, headers={
            'X-GitHub-Event': event, 'X-Hub-Signature-256': 'sha256=' + signature})


def push_payload():
    return dict(installation={'id': 10}, repository={'id': 101, 'private': False},
                ref='refs/heads/main', after='a' * 40)


def test_push_dispatch_uses_background_service_without_route_token_request(app):
    payload = push_payload()
    response = send(app, 'push', payload)
    assert response.status_code == 202
    app.state.ingestion_service.handle_github_push_webhook.assert_awaited_once_with(payload)
    app.state.github_app.indexing_credential.assert_not_awaited()


@pytest.mark.parametrize('updates', [
    {'ref': 'refs/heads/feature'}, {'ref': None}, {'after': None}, {'after': '0' * 40},
    {'repository': {'id': 101, 'private': True}}, {'installation': {'id': 99}},
])
def test_irrelevant_push_is_ignored_even_without_ingestion_service(app, updates):
    payload = push_payload()
    payload.update(updates)
    del app.state.ingestion_service
    response = send(app, 'push', payload)
    assert response.status_code == 200 and response.json()['status'] == 'ignored'


def test_relevant_push_requires_initialized_service(app):
    del app.state.ingestion_service
    assert send(app, 'push', push_payload()).status_code == 503


@pytest.mark.parametrize('event,action', [('installation', 'unknown'), ('installation_repositories', 'unknown')])
def test_unsupported_installation_actions_do_not_reconcile(app, event, action):
    response = send(app, event, {'installation': {'id': 10}, 'action': action})
    assert response.json()['status'] == 'ignored'
    app.state.github_app.invalidate.assert_not_called()
    app.state.github_app.reconcile.assert_not_awaited()


def test_installation_errors_keep_http_status_translation(app):
    app.state.github_app.reconcile.side_effect = GitHubAppError(403, 'Installation access denied')
    response = send(app, 'installation', {'installation': {'id': 10}, 'action': 'created'})
    assert response.status_code == 403
    assert response.json()['detail'] == 'Installation access denied'
    app.state.github_app.invalidate.assert_called_once_with('10')


def test_unknown_event_keeps_existing_installation_validation_order(app):
    assert send(app, 'unknown', {}).status_code == 400
    response = send(app, 'unknown', {'installation': {'id': 10}})
    assert response.json() == {'status': 'ignored', 'event': 'unknown'}


def test_ping_needs_neither_installation_nor_ingestion_service(app):
    del app.state.ingestion_service
    assert send(app, 'ping', {}).json() == {'status': 'ok', 'event': 'ping'}
