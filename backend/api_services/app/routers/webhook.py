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


@router.post('')
@router.post('/github')
async def github_webhook(request: Request, background_tasks: BackgroundTasks,
                         x_github_event: str | None = Header(None, alias='X-GitHub-Event'),
                         x_hub_signature_256: str | None = Header(None, alias='X-Hub-Signature-256')):
    configured = getattr(request.app.state, 'github_app', None)
    secret = configured.settings.webhook_secret if configured else GITHUB_APP_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(500, 'Webhook secret is not configured')
    raw = await request.body()
    if not verify_github_signature(raw, x_hub_signature_256, secret):
        raise HTTPException(401, 'Invalid GitHub webhook signature')
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Malformed webhook JSON object') from None
    event = x_github_event or 'unknown'
    if event == 'ping':
        return {'status': 'ok', 'event': 'ping'}
    app_service = get_github_app(request)
    try:
        if event == 'github_app_authorization':
            sender = payload.get('sender')
            if payload.get('action') != 'revoked' or not isinstance(sender, dict) or not isinstance(sender.get('id'), int):
                raise HTTPException(400, 'Invalid authorization event')
            # A still-valid current token may reflect reauthorization after a late event.
            connection = None
            from sqlalchemy import select
            from ai_services.ingestion.persistence.models import GitHubConnectionModel
            with app_service.store.session_factory() as session:
                connection = session.scalars(select(GitHubConnectionModel).where(
                    GitHubConnectionModel.github_user_id == str(sender['id']))).first()
            if connection:
                try:
                    token = app_service.user_token(connection.user_id)
                    await app_service.request('GET', '/user', token)
                except GitHubAppError as exc:
                    if exc.status_code in {401, 403}:
                        app_service.revoke_user(str(sender['id']))
                    else:
                        raise
            return {'status': 'reconciled', 'event': event}
        installation = payload.get('installation')
        if not isinstance(installation, dict) or not isinstance(installation.get('id'), int):
            raise HTTPException(400, 'Webhook installation ID is required')
        iid = str(installation['id'])
        if event in {'installation', 'installation_repositories'}:
            supported = {'created', 'deleted', 'suspend', 'unsuspend', 'new_permissions_accepted'} if event == 'installation' else {'added', 'removed'}
            if payload.get('action') not in supported:
                return {'status': 'ignored', 'event': event}
            # Full current inventory, not incremental payload deltas. Safe for duplicate/late delivery.
            # Permissions may change without changing repository inventory.
            app_service.invalidate(iid)
            await app_service.reconcile(iid)
            return {'status': 'reconciled', 'event': event}
        if event not in {'push', 'pull_request'}:
            return {'status': 'ignored', 'event': event}
        repo = payload.get('repository')
        if not isinstance(repo, dict) or not isinstance(repo.get('id'), int):
            raise HTTPException(400, 'Webhook repository ID is required')
        tracked = app_service.store.get_repository(str(repo['id']))
        if not tracked or tracked.installation_id != iid or repo.get('private', True):
            return {'status': 'ignored', 'event': event}
        # Ingestion reacquires/rechecks installation credentials when BackgroundTasks executes.
        if event == 'push':
            ref = payload.get('ref')
            if not isinstance(ref, str) or ref != 'refs/heads/' + tracked.tracked_branch:
                return {'status': 'ignored', 'event': event}
            if not payload.get('after') or payload['after'] == '0' * 40:
                return {'status': 'ignored', 'event': event}
        service = getattr(request.app.state, 'ingestion_service', None)
        if service is None:
            raise HTTPException(503, 'Repository ingestion service is not initialized')
        if event == 'push':
            background_tasks.add_task(_process_push_in_background, service, payload)
            return JSONResponse(status_code=202, content={'status': 'accepted', 'event': event})
        if not isinstance(payload.get('pull_request'), dict):
            raise HTTPException(400, 'Invalid pull request event')
        await app_service.indexing_credential(tracked.id, iid)
        result = await service.handle_github_pull_request_webhook(payload)
        return {'status': result.status, 'event': event, 'repository': result.repository,
                'message': result.message, 'indexed_commit_sha': result.indexed_commit_sha}
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
