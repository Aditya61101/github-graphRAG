from __future__ import annotations

import hashlib
import hmac
import json
import logging
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from ai_services.github_app.service import GitHubAppError
from api_services.app.config import GITHUB_APP_WEBHOOK_SECRET
from api_services.app.utils.github_app_access import get_github_app
from ai_services.ingestion.pr.models import PullRequestEvent
from pydantic import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_github_signature(raw_body, signature_header, secret):
    if not secret or not signature_header or not signature_header.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def _process_push_in_background(service, payload):
    try:
        await service.handle_github_push_webhook(payload)
    except Exception:
        logger.exception('Webhook indexing failed')


async def _process_pr_in_background(service, event):
    try:
        print('processing pr in background: ', event.model_dump_json(indent=2))
        pr_request_change_set = await service.ingest_pull_request(event)
        print('pr request change set: ', pr_request_change_set.model_dump_json(indent=2))
    except Exception as exc:
        logger.error(
            'PR background ingestion failed github_repository=%s pr=%s base=%s head=%s delivery=%s error_type=%s',
            event.github_repository_id, event.pull_request_number, event.base_sha, event.head_sha,
            event.delivery_id, type(exc).__name__
        )


PR_INGESTION_ACTIONS = {'opened', 'synchronize', 'reopened'}
INSTALLATION_ACTIONS = {
    'installation': {'created', 'deleted', 'suspend', 'unsuspend', 'new_permissions_accepted'},
    'installation_repositories': {'added', 'removed'},
}


async def _verified_payload(request, signature):
    configured = getattr(request.app.state, 'github_app', None)
    secret = configured.settings.webhook_secret if configured else GITHUB_APP_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(500, 'Webhook secret is not configured')
    raw = await request.body()
    if not verify_github_signature(raw, signature, secret):
        raise HTTPException(401, 'Invalid GitHub webhook signature')
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Malformed webhook JSON object') from None


def _is_merged_pr(payload):
    pr = payload.get('pull_request')
    return payload.get('action') == 'closed' and isinstance(pr, dict) and pr.get('merged') is True


def _parse_pr_event(payload, delivery_id):
    try:
        return PullRequestEvent.from_webhook(payload, delivery_id)
    except (ValueError, ValidationError):
        raise HTTPException(400, 'Invalid or incomplete pull request webhook metadata') from None


def _installation_id(payload):
    installation = payload.get('installation')
    if not isinstance(installation, dict) or not isinstance(installation.get('id'), int):
        raise HTTPException(400, 'Webhook installation ID is required')
    return str(installation['id'])


def _tracked_repository(app_service, payload, installation_id):
    repo = payload.get('repository')
    if not isinstance(repo, dict) or not isinstance(repo.get('id'), int):
        raise HTTPException(400, 'Webhook repository ID is required')
    tracked = app_service.store.get_repository(str(repo['id']))
    if not tracked or tracked.installation_id != installation_id or repo.get('private', True):
        return None
    return tracked


async def _handle_authorization(app_service, payload, event):
    from sqlalchemy import select
    from ai_services.ingestion.persistence.models import GitHubConnectionModel

    sender = payload.get('sender')
    if payload.get('action') != 'revoked' or not isinstance(sender, dict) or not isinstance(sender.get('id'), int):
        raise HTTPException(400, 'Invalid authorization event')
    # A still-valid current token may reflect reauthorization after a late event.
    with app_service.store.session_factory() as session:
        connection = session.scalars(select(GitHubConnectionModel).where(
            GitHubConnectionModel.github_user_id == str(sender['id']))).first()
    if connection:
        try:
            token = app_service.user_token(connection.user_id)
            await app_service.request('GET', '/user', token)
        except GitHubAppError as exc:
            if exc.status_code not in {401, 403}:
                raise
            app_service.revoke_user(str(sender['id']))
    return {'status': 'reconciled', 'event': event}


async def _handle_installation(app_service, payload, event, installation_id):
    if payload.get('action') not in INSTALLATION_ACTIONS[event]:
        return {'status': 'ignored', 'event': event}
    # Reconcile current inventory, never apply potentially stale payload deltas.
    app_service.invalidate(installation_id)
    await app_service.reconcile(installation_id)
    return {'status': 'reconciled', 'event': event}


def _queue_pr(request, background_tasks, normalized_pr, tracked):
    if (normalized_pr.base_repository_id != normalized_pr.github_repository_id
            or normalized_pr.repository_full_name.lower() != tracked.full_name.lower()):
        raise HTTPException(400, 'Pull request repository identity mismatch')
    service = getattr(request.app.state, 'pr_ingestion_service', None)
    if service is None:
        raise HTTPException(503, 'Pull request ingestion service is not initialized')
    background_tasks.add_task(_process_pr_in_background, service, normalized_pr)
    return JSONResponse(
        status_code=202, 
        content={'status': 'accepted', 'event': 'pull_request',
        'repository_id': tracked.id, 'pull_request_number': normalized_pr.pull_request_number,
        'revision_key': normalized_pr.revision_key}
    )


def _queue_push(request, background_tasks, payload, tracked):
    ref = payload.get('ref')
    if (not isinstance(ref, str) or ref != 'refs/heads/' + tracked.tracked_branch
            or not payload.get('after') or payload['after'] == '0' * 40):
        return {'status': 'ignored', 'event': 'push'}
    service = getattr(request.app.state, 'ingestion_service', None)
    if service is None:
        raise HTTPException(503, 'Repository ingestion service is not initialized')
    # Execution-time ingestion reacquires and rechecks installation credentials.
    background_tasks.add_task(_process_push_in_background, service, payload)
    return JSONResponse(status_code=202, content={'status': 'accepted', 'event': 'push'})


async def _acknowledge_merged_pr(request, app_service, payload, tracked, installation_id):
    service = getattr(request.app.state, 'ingestion_service', None)
    if service is None:
        raise HTTPException(503, 'Repository ingestion service is not initialized')
    await app_service.indexing_credential(tracked.id, installation_id)
    result = await service.handle_github_pull_request_webhook(payload)
    return {'status': result.status, 'event': 'pull_request', 'repository': result.repository,
            'message': result.message, 'indexed_commit_sha': result.indexed_commit_sha}


@router.post('')
@router.post('/github')
async def github_webhook(request: Request, background_tasks: BackgroundTasks,
                         x_github_event: str | None = Header(None, alias='X-GitHub-Event'),
                         x_hub_signature_256: str | None = Header(None, alias='X-Hub-Signature-256'),
                         x_github_delivery: str | None = Header(None, alias='X-GitHub-Delivery')):
    payload = await _verified_payload(request, x_hub_signature_256)
    event = x_github_event or 'unknown'
    if event == 'ping':
        return {'status': 'ok', 'event': 'ping'}

    normalized_pr = None
    if event == 'pull_request':
        if payload.get('action') in PR_INGESTION_ACTIONS:
            normalized_pr = _parse_pr_event(payload, x_github_delivery)
        elif not _is_merged_pr(payload):
            return {'status': 'ignored', 'event': event}

    app_service = get_github_app(request)
    try:
        if event == 'github_app_authorization':
            return await _handle_authorization(app_service, payload, event)
        installation_id = _installation_id(payload)
        if event in INSTALLATION_ACTIONS:
            return await _handle_installation(app_service, payload, event, installation_id)
        if event not in {'push', 'pull_request'}:
            return {'status': 'ignored', 'event': event}

        tracked = _tracked_repository(app_service, payload, installation_id)
        if tracked is None:
            return {'status': 'ignored', 'event': event}
        if normalized_pr is not None:
            return _queue_pr(request, background_tasks, normalized_pr, tracked)
        if event == 'push':
            return _queue_push(request, background_tasks, payload, tracked)
        # Only merged PRs reach this point; accepted RKG updates remain push-driven.
        return await _acknowledge_merged_pr(request, app_service, payload, tracked, installation_id)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
