import asyncio
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select

from ai_services.ingestion.persistence.models import (
    ADRModel,
    IngestionRunModel,
    PullRequestRevisionModel,
)
from ai_services.ingestion.pr.models import PullRequestEvent
from ai_services.tests.test_github_app import env as env  # noqa: PLC0414 - pytest fixture re-export
from ai_services.tests.test_github_app import track, webhook
from api_services.app import config
from api_services.app.routers.health import router as health_router
from ai_services.decision_guard.orchestration.dispatch import EvaluationOrchestrator
from ai_services.decision_guard.orchestration.worker import EvaluationWorker


@pytest.fixture
def disabled(env, monkeypatch):
    repo = asyncio.run(track(env))
    monkeypatch.setattr(config, 'ENABLE_BACKGROUND_PROCESSING', False)
    env.app.state.pr_ingestion_service = SimpleNamespace(ingest_pull_request=AsyncMock())
    env.app.state.adr_service = SimpleNamespace(process_adr_uploads=AsyncMock())
    env.app.include_router(health_router, prefix='/health')
    env.app.state.graph_service = SimpleNamespace(
        count_architectural_decisions=AsyncMock(return_value=0),
        get_repository_graph=AsyncMock(return_value={'nodes': [], 'edges': []}),
    )
    tasks = MagicMock(side_effect=AssertionError('Disabled processing must not submit tasks'))
    monkeypatch.setattr(BackgroundTasks, 'add_task', tasks)
    claims = {}
    for name in ('record_ingestion_start', 'claim_pr_revision', 'claim_or_create_adr'):
        claims[name] = MagicMock(side_effect=AssertionError('Disabled processing must not claim work'))
        monkeypatch.setattr(env.store, name, claims[name])
    return SimpleNamespace(env=env, repo=repo, tasks=tasks, claims=claims)


def assert_no_work(disabled):
    env = disabled.env
    disabled.tasks.assert_not_called()
    for claim in disabled.claims.values():
        claim.assert_not_called()
    env.app.state.ingestion_service.ingest_repository.assert_not_awaited()
    env.app.state.ingestion_service.handle_github_push_webhook.assert_not_awaited()
    env.app.state.pr_ingestion_service.ingest_pull_request.assert_not_awaited()
    env.app.state.adr_service.process_adr_uploads.assert_not_awaited()
    with env.store.session_factory() as session:
        for model in (IngestionRunModel, PullRequestRevisionModel, ADRModel):
            assert session.scalars(select(model)).first() is None


@pytest.mark.parametrize('path', ['/repositories/ingest', '/repositories/owner/repo101/sync'])
@pytest.mark.parametrize('sync', [False, True])
def test_disabled_ingest_and_sync_create_no_work(disabled, path, sync, monkeypatch):
    bind = MagicMock(side_effect=AssertionError('Disabled ingestion must not bind'))
    monkeypatch.setattr(disabled.env.service, 'bind_repository', bind)
    response = disabled.env.client.post(path, headers=disabled.env.headers,
        json={'repository': 'owner/repo101', 'sync': sync, 'force_full': True})
    assert response.status_code == 503
    assert 'No processing was queued' in response.json()['detail']
    bind.assert_not_called()
    assert_no_work(disabled)


def test_disabled_adr_upload_does_not_claim_or_process(disabled):
    response = disabled.env.client.post('/repositories/' + disabled.repo.id + '/adrs',
        headers=disabled.env.headers, files={'files': ('source.md', b'PRIVATE-SOURCE', 'text/markdown')})
    assert response.status_code == 503
    assert_no_work(disabled)


def test_disabled_continuation_preserves_job_and_read_apis(disabled):
    env = disabled.env
    store = MagicMock()
    store.list.return_value = []
    env.app.state.evaluation_orchestrator = SimpleNamespace(store=store)
    base = '/repositories/' + disabled.repo.id + '/evaluations'
    response = env.client.post(base + '/job/continue', headers=env.headers, json={})
    assert response.status_code == 503
    store.continue_job.assert_not_called()
    assert env.client.get(base, headers=env.headers).status_code == 200
    assert_no_work(disabled)


def event_payload(action='opened'):
    return {'action': action, 'installation': {'id': 10},
        'repository': {'id': 101, 'full_name': 'owner/repo101', 'private': False},
        'pull_request': {'number': 42, 'title': 'PRIVATE-TITLE', 'body': 'PRIVATE-BODY',
            'base': {'sha': 'a' * 40, 'ref': 'main', 'repo': {'id': 101}},
            'head': {'sha': 'b' * 40, 'ref': 'feature', 'repo': {'id': 202, 'full_name': 'fork/repo'}}}}


@pytest.mark.parametrize('action', ['opened', 'synchronize', 'reopened'])
def test_disabled_pr_webhooks_do_not_submit_or_claim(disabled, action):
    response = webhook(disabled.env, 'pull_request', event_payload(action))
    assert response.status_code == 503
    assert_no_work(disabled)


def test_disabled_push_webhook_does_not_submit_or_claim(disabled):
    payload = {'installation': {'id': 10}, 'repository': {'id': 101, 'private': False},
        'ref': 'refs/heads/main', 'after': 'c' * 40}
    response = webhook(disabled.env, 'push', payload)
    assert response.status_code == 503
    assert_no_work(disabled)
    payload['ref'] = 'refs/heads/untracked'
    assert webhook(disabled.env, 'push', payload).json()['status'] == 'ignored'
    assert_no_work(disabled)


def test_disabled_merged_and_ignored_pr_events_are_honest_metadata_only(disabled):
    payload = event_payload('closed')
    payload['pull_request']['merged'] = True
    response = webhook(disabled.env, 'pull_request', payload)
    assert response.status_code == 200
    assert response.json()['status'] == 'acknowledged'
    assert 'no synchronization was queued' in response.json()['message']
    payload['pull_request']['merged'] = False
    assert webhook(disabled.env, 'pull_request', payload).json()['status'] == 'ignored'
    assert_no_work(disabled)


def test_disabled_processing_preserves_auth_metadata_dashboard_and_queries(disabled):
    env = disabled.env
    for path in ('/health', '/auth/me', '/auth/onboarding', '/repositories', '/repositories/tracked',
                 '/repositories/owner/repo101', '/repositories/' + disabled.repo.id + '/overview',
                 '/repositories/' + disabled.repo.id + '/graph'):
        assert env.client.get(path, headers=env.headers).status_code == 200
    assert env.client.get('/auth/github/login').status_code == 302
    response = env.client.post('/query', headers=env.headers,
        json={'query': 'question', 'conversation_id': 'thread', 'repository_id': disabled.repo.id})
    assert response.status_code == 200
    assert response.json()['answer'] == 'grounded'
    assert env.client.post('/auth/github/install', headers=env.headers).status_code == 200
    response = env.client.post('/repositories/' + disabled.repo.id + '/query', headers=env.headers,
        json={'query': 'question', 'conversation_id': 'repo-thread'})
    assert response.status_code == 200 and response.json()['answer'] == 'grounded'
    assert_no_work(disabled)


def test_disabled_webhooks_keep_signature_verification_and_metadata_reconciliation(disabled):
    env = disabled.env
    assert webhook(env, 'pull_request', event_payload(), signature=False).status_code == 401
    response = webhook(env, 'installation', {'action': 'created', 'installation': {'id': 10}})
    assert response.status_code == 200 and response.json()['status'] == 'reconciled'
    response = webhook(env, 'github_app_authorization', {'action': 'revoked', 'sender': {'id': 1}})
    assert response.status_code == 200 and response.json()['status'] == 'reconciled'
    assert webhook(env, 'ping', {}).json()['status'] == 'ok'
    assert_no_work(disabled)


@pytest.mark.parametrize('result', [None, SimpleNamespace(files=['PRIVATE-SOURCE'], patch='PRIVATE-PATCH')])
@pytest.mark.asyncio
async def test_pr_logging_never_serializes_source_and_handles_duplicates(result, caplog, capsys):
    event = PullRequestEvent.from_webhook(event_payload(), 'delivery')
    store = MagicMock()
    dispatcher = SimpleNamespace(dispatch=AsyncMock(return_value=result))
    service = EvaluationOrchestrator(store, MagicMock(), dispatcher=dispatcher)
    with caplog.at_level(logging.INFO):
        await service._dispatch_persisted(event.revision_key)
    output = caplog.text + capsys.readouterr().out
    dispatcher.dispatch.assert_awaited_once_with(event.revision_key)
    store.dispatched.assert_called_once_with(event.revision_key)
    for value in ('PRIVATE-SOURCE', 'PRIVATE-PATCH', 'PRIVATE-TITLE', 'PRIVATE-BODY'):
        assert value not in output
    assert 'failed' not in output


@pytest.mark.asyncio
async def test_pr_logging_does_not_expose_exception_message(caplog, capsys):
    store = MagicMock()
    service = EvaluationOrchestrator(store, MagicMock(), dispatcher=SimpleNamespace(
        dispatch=AsyncMock(side_effect=RuntimeError('PRIVATE-PATCH ACCESS-TOKEN'))))
    with caplog.at_level(logging.WARNING):
        await service._dispatch_persisted('job')
    store.dispatched.assert_not_called()
    output = caplog.text + capsys.readouterr().out
    assert 'error_type=RuntimeError' in output
    assert 'PRIVATE-PATCH' not in output and 'ACCESS-TOKEN' not in output


@pytest.mark.asyncio
async def test_disabled_worker_and_recovery_do_not_claim_or_dispatch(monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_BACKGROUND_PROCESSING', False)
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'false')
    store = MagicMock()
    worker = EvaluationWorker(store, MagicMock())
    publisher = SimpleNamespace(reconcile=AsyncMock())
    dispatcher = SimpleNamespace(dispatch=AsyncMock())
    service = EvaluationOrchestrator(store, worker, publisher=publisher, dispatcher=dispatcher)
    await worker.run_job('job')
    await service.reconcile()
    await service._dispatch_persisted('job')
    with pytest.raises(RuntimeError, match='disabled'):
        await service.accept('repo', object(), MagicMock())
    store.claim.assert_not_called()
    store.due.assert_not_called()
    store.enqueue.assert_not_called()
    publisher.reconcile.assert_not_awaited()
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_direct_ingestion_entrypoints_stop_before_any_dependencies(monkeypatch):
    from ai_services.ingestion.service import RepositoryIngestionService
    from ai_services.ingestion.pr.service import PullRequestIngestionService
    from ai_services.ingestion.adr.service import ADRService
    from ai_services.ingestion.adr.processor import ADRProcessingService
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'false')
    calls = [
        RepositoryIngestionService.ingest_repository(None, 'repo'),
        RepositoryIngestionService.handle_github_push_webhook(None, {}),
        PullRequestIngestionService.ingest_pull_request(None, None),
        ADRService.process_adr_uploads(None, 'repo', 'user', []),
        ADRService.process_adr_upload(None, 'repo', 'user', 'adr.md', b'body'),
        ADRProcessingService.process_adr(None, None),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match='disabled'):
            await call


@pytest.mark.parametrize('value,expected', [('true', True), ('1', True), ('yes', True), ('on', True),
    ('false', False), ('0', False), ('no', False), ('off', False), (' FALSE ', False)])
def test_processing_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', value)
    assert config.background_processing_enabled() is expected


def test_processing_default_and_invalid_config(monkeypatch):
    monkeypatch.delenv('ENABLE_BACKGROUND_PROCESSING', raising=False)
    assert config.background_processing_enabled()
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'typo')
    with pytest.raises(RuntimeError, match='ENABLE_BACKGROUND_PROCESSING'):
        config.background_processing_enabled()


@pytest.mark.parametrize('path,excluded', [
    ('Dockerfile', False), ('cloudbuild.yaml', False), ('.dockerignore', False),
    ('backend/pyproject.toml', False), ('backend/uv.lock', False),
    ('backend/api_services/app/main.py', False), ('backend/api_services/app/utils/processing.py', False),
    ('backend/ai_services/ingestion/persistence/postgres_schema.py', False), ('backend/shared/utils/env_helper.py', False),
    ('backend/.env', True), ('backend/.env.production', True), ('backend/secrets/github.pem', True),
    ('backend/api_services/app/key.pem', True), ('backend/shared/credentials.json', True),
    ('backend/ai_services/client_secret_example.json', True), ('backend/data/database.db', True),
    ('backend/ai_services/.state/candidates.jsonl', True), ('backend/ai_services/data/repos/a/b/file.py', True),
    ('backend/.venv/file.py', True), ('backend/ai_services/tests/test_file.py', True),
    ('backend/ai_services/__pycache__/file.pyc', True), ('backend/ai_services/token.json', True),
    ('backend/shared/signing.PEM', True), ('backend/api_services/id_rsa', True),
    ('backend/ai_services/database.dump', True), ('backend/shared/service-account-key.json', True),
])
def test_gcloud_upload_filter_keeps_build_inputs_and_excludes_sensitive_paths(tmp_path, path, excluded):
    project = Path(__file__).resolve().parents[3]
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text((project / '.gcloudignore').read_text())
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('synthetic-test-data')
    result = subprocess.run(['git', '-C', str(tmp_path), 'check-ignore', '--no-index', '-q', path], check=False)
    assert result.returncode == (0 if excluded else 1)
