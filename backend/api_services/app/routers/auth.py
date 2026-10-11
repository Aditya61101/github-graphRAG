from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import secrets
import time
from urllib.parse import urlencode

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ai_services.github_app.service import GitHubAppError, as_utc, oauth_credentials
from ai_services.ingestion.persistence.models import UserModel, GitHubConnectionModel
from api_services.app.utils.github_app_access import get_github_app
from api_services.app.utils.jwt_utils import create_access_token, get_current_user

router = APIRouter()


@dataclass(frozen=True)
class OAuthTransaction:
    browser: str
    expires_at: float
    kind: str
    user_id: str | None = None
    github_user_id: str | None = None


@dataclass
class InstallationReturn:
    browser: str
    expires_at: float
    outcome: str
    installation_id: str | None = None
    verified_return: bool = False


class GitHubOAuth:
    """Temporary one-use transactions, not application JWT sessions."""
    def __init__(self, settings):
        self.settings = settings
        self.transactions: dict[str, OAuthTransaction] = {}
        self.consumed: dict[str, float] = {}
        self.installation_returns: dict[str, InstallationReturn] = {}
        oauth = OAuth()
        oauth.register(name='github', client_id=settings.client_id, client_secret=settings.client_secret,
            access_token_url='https://github.com/login/oauth/access_token',
            authorize_url='https://github.com/login/oauth/authorize',
            client_kwargs={'code_challenge_method': 'S256'},
            access_token_params={'client_id': settings.client_id, 'client_secret': settings.client_secret},
            token_endpoint_auth_method='client_secret_post')
        self.client = oauth.github

    def create(self, request, kind, user_id=None, github_user_id=None):
        self.consumed = {key: value for key, value in self.consumed.items() if value > time.time()}
        for key, value in list(self.transactions.items()):
            if value.expires_at < time.time():
                del self.transactions[key]
        self.installation_returns = {key: value for key, value in self.installation_returns.items()
                                     if value.expires_at > time.time()}
        browser = request.session.setdefault('oauth_browser', secrets.token_urlsafe(32))
        state = secrets.token_urlsafe(32)
        self.transactions[state] = OAuthTransaction(browser, time.time() + 600, kind, user_id, github_user_id)
        return state

    def consume(self, request):
        state = request.query_params.get('state')
        txn = self.transactions.pop(state, None)
        if txn:
            self.consumed[state] = time.time() + 600
        if not txn or txn.expires_at < time.time() or not secrets.compare_digest(
                txn.browser, request.session.get('oauth_browser', '')):
            raise HTTPException(400, 'Invalid, expired, or replayed OAuth state')
        return txn


def get_oauth(request):
    value = getattr(request.app.state, 'github_oauth', None)
    if value is None:
        raise HTTPException(503, 'GitHub OAuth is not initialized')
    return value


@router.get('/github/login')
async def github_login(request: Request):
    flow = get_oauth(request)
    state = flow.create(request, 'login')
    return await flow.client.authorize_redirect(request, flow.settings.callback_url,
                                                state=state, code_verifier=secrets.token_urlsafe(64))


@router.post('/github/install')
async def installation_start(request: Request, current_user: UserModel = Depends(get_current_user)):
    flow = get_oauth(request)
    connection = get_github_app(request).store.get_user_github_connection(current_user.id)
    if not connection or connection.credential_kind != 'github_app':
        raise HTTPException(409, 'GitHub reconnect required')
    state = flow.create(request, 'installation', current_user.id, connection.github_user_id)
    # A new attempt supersedes a previous return, including retryable success.
    flow.installation_returns.pop(current_user.id, None)
    # Automatic post-install authorization has no explicit-login PKCE challenge.
    # Register Authlib state and enforce the browser-bound one-use transaction.
    await flow.client.save_authorize_data(request, state=state, redirect_uri=flow.settings.callback_url)
    return {'installation_url': f'https://github.com/apps/{flow.settings.slug}/installations/new?' + urlencode({'state': state})}


def onboarding_status(user):
    completed = user.onboarding_completed_at is not None
    return {'onboarding_completed': completed,
            'onboarding_completed_at': user.to_dict()['onboarding_completed_at'],
            'next': '/projects' if completed else '/install'}


def installation_return(request, user_id):
    result = get_oauth(request).installation_returns.get(user_id)
    if (result and result.expires_at > time.time() and secrets.compare_digest(
            result.browser, request.session.get('oauth_browser', ''))):
        return result
    return None


def callback_redirect(flow, user, outcome=None):
    params = {'token': create_access_token({'sub': user.id}), 'next': onboarding_status(user)['next']}
    if outcome:
        params['installation_outcome'] = outcome
    # Existing prototype delivery; never log the URL or its bearer token.
    return RedirectResponse(flow.settings.frontend_url + '/auth/success?' + urlencode(params))


async def verify_installation_return(service, user_id, result):
    """Only a validated successful return is eligible for automatic completion."""
    try:
        summaries, repositories = await service.discovery(user_id)
    except GitHubAppError as exc:
        result.outcome = ('reauthorization_required' if exc.reconnect_required else
                          'temporary_failure' if exc.status_code in {401, 429, 502, 503, 504} else 'access_denied')
        return service.store.get_user(user_id)
    if not any(i['id'] == result.installation_id for i in summaries):
        # Absence from user discovery is not proof of pending organization approval.
        result.outcome = 'access_denied'
    elif not any(r['installation_id'] == result.installation_id for r in repositories):
        result.outcome = 'no_eligible_repositories'
    else:
        user = service.store.complete_onboarding(user_id)
        result.outcome = 'success'
        return user
    return service.store.get_user(user_id)


@router.get('/github/callback')
async def github_callback(request: Request):
    flow = get_oauth(request)
    if request.query_params.get('state') in flow.consumed:
        raise HTTPException(400, 'Replayed OAuth state')
    # Ignore direct-GitHub codes without a local transaction; start protected OAuth.
    # Never trust installation_id/setup_action as authorization.
    if not request.query_params.get('state') and request.query_params.get('code'):
        return await github_login(request)
    if (request.query_params.get('installation_id') and
            request.query_params.get('state') not in flow.transactions):
        return await github_login(request)
    txn = flow.consume(request)
    error = request.query_params.get('error')
    code = request.query_params.get('code')
    service = get_github_app(request)
    if txn.kind == 'installation':
        user = service.store.get_user(txn.user_id)
        connection = service.store.get_user_github_connection(txn.user_id)
        if not user or not connection or connection.github_user_id != txn.github_user_id:
            raise HTTPException(403, 'Installation account binding changed; restart installation')
        hint = request.query_params.get('installation_id')
        action = request.query_params.get('setup_action')
        result = InstallationReturn(txn.browser, time.time() + 600, 'cancelled', hint)
        flow.installation_returns[user.id] = result
        if error:
            result.outcome = ('cancelled' if error in {'access_denied', 'user_denied'} else
                              'temporary_failure' if error in {'server_error', 'temporarily_unavailable'} else
                              'authorization_failed')
            return callback_redirect(flow, user, result.outcome)
        if action == 'request':
            result.outcome = 'pending_approval'
            return callback_redirect(flow, user, result.outcome)
        if not hint or not hint.isdigit() or action not in {'install', 'update'}:
            # Back-navigation or an incomplete callback cannot reuse old grants.
            return callback_redirect(flow, user, result.outcome)
        # With no authorization code, the one-use browser/account-bound return
        # uses the initiating user's stored credentials through normal refresh.
        if not code:
            result.verified_return = True
            user = await verify_installation_return(service, user.id, result)
            return callback_redirect(flow, user, result.outcome)
    if error:
        raise HTTPException(400, 'GitHub authorization was cancelled or denied')
    if not code:
        raise HTTPException(400, 'Missing GitHub authorization code')
    app_service = get_github_app(request)
    try:
        issued_at = datetime.now(timezone.utc)
        token = await flow.client.authorize_access_token(request)
        if not token.get('access_token') or not token.get('expires_in'):
            raise HTTPException(400, 'Expiring GitHub App user tokens are required')
        identity = await app_service.request('GET', '/user', token['access_token'])
        if not isinstance(identity, dict) or type(identity.get('id')) is not int or identity['id'] < 1 or not identity.get('login'):
            raise HTTPException(502, 'Invalid GitHub identity response')
        if txn.user_id:
            existing = app_service.store.get_user_github_connection(txn.user_id)
            if not existing or existing.github_user_id != str(identity['id']):
                raise HTTPException(403, 'Installation authorization must use the initiating GitHub account')
        access, refresh, expiry, refresh_expiry = oauth_credentials(token, issued_at)
        # Reauthorization and refresh for an existing account must not overwrite
        # each other's rotated credentials in the single-worker runtime.
        existing = app_service.store.get_user_github_connection(txn.user_id) if txn.user_id else None
        if not existing:
            with app_service.store.session_factory() as session:
                existing = session.scalars(select(GitHubConnectionModel).where(
                    GitHubConnectionModel.github_user_id == str(identity['id']))).first()
        def save_login():
            return app_service.store.upsert_github_login(
                github_user_id=str(identity['id']), username=identity['login'], email=identity.get('email'),
                avatar_url=identity.get('avatar_url'), access_token=access, expires_at=expiry,
                refresh_token=refresh, refresh_expires_at=refresh_expiry)
        if existing:
            async with app_service.user_locks.setdefault(existing.user_id, asyncio.Lock()):
                user, _ = save_login()
        else:
            user, _ = save_login()
        if txn.kind == 'installation':
            result.verified_return = True
            user = await verify_installation_return(app_service, user.id, result)
    except GitHubAppError as exc:
        if txn.kind == 'installation':
            result.outcome = discovery_error(exc).detail['outcome']
            return callback_redirect(flow, service.store.get_user(txn.user_id), result.outcome)
        raise HTTPException(exc.status_code, str(exc)) from None
    except HTTPException:
        raise
    except httpx.HTTPError:
        if txn.kind == 'installation':
            result.outcome = 'temporary_failure'
            return callback_redirect(flow, service.store.get_user(txn.user_id), result.outcome)
        raise HTTPException(503, 'GitHub authorization temporarily unavailable') from None
    except Exception:
        if txn.kind == 'installation':
            result.outcome = 'authorization_failed'
            return callback_redirect(flow, service.store.get_user(txn.user_id), result.outcome)
        raise HTTPException(400, 'GitHub authorization failed; restart login') from None
    return callback_redirect(flow, user, result.outcome if txn.kind == 'installation' else None)


@router.get('/me')
async def me(request: Request, current_user: UserModel = Depends(get_current_user)):
    service = get_github_app(request)
    connection = service.store.get_user_github_connection(current_user.id)
    reconnect = not connection or connection.credential_kind != 'github_app' or connection.access_status != 'ACTIVE'
    if not reconnect and connection.access_token_expires_at:
        now = datetime.now(timezone.utc)
        access_expired = as_utc(connection.access_token_expires_at) <= now
        refresh_available = (connection.refresh_token and connection.refresh_token_expires_at and
                             as_utc(connection.refresh_token_expires_at) > now)
        reconnect = bool(access_expired and not refresh_available)
    return {
        'user': current_user.to_dict(),
        **onboarding_status(current_user),
        'installations': [],
        'installation_onboarding_required': current_user.onboarding_completed_at is None,
        'reconnect_required': reconnect,
        'installation_discovery_status': 'not_requested',
    }


@router.get('/onboarding')
async def onboarding(request: Request, current_user: UserModel = Depends(get_current_user)):
    service = get_github_app(request)
    try:
        installations, repositories = await service.discovery(current_user.id)
    except GitHubAppError as exc:
        raise discovery_error(exc) from None
    result = installation_return(request, current_user.id)
    return {**onboarding_status(current_user), 'eligible': bool(repositories),
            'screen': 'completed' if current_user.onboarding_completed_at else 'available_repositories' if repositories else 'install',
            'installation_discovery_status': 'complete',
            'installation_outcome': result.outcome if result else None,
            'installations': [{**item, 'repositories': [
                {'github_repository_id': str(repo['id']), 'installation_id': repo['installation_id'],
                 'full_name': repo['full_name'], 'repository_url': repo['html_url'],
                 'name': repo['name'], 'is_private': False}
                for repo in repositories if repo['installation_id'] == item['id']]} for item in installations]}


def discovery_error(exc):
    outcome = ('reauthorization_required' if exc.reconnect_required else
               'temporary_failure' if exc.status_code in {401, 429, 502, 503, 504} else 'access_denied')
    # Application JWT remains valid when GitHub credentials need reconnection.
    status = 409 if exc.reconnect_required else 502 if exc.status_code == 401 else exc.status_code
    return HTTPException(status, {'outcome': outcome, 'message': str(exc)})


@router.post('/onboarding/complete')
async def complete_onboarding(request: Request, current_user: UserModel = Depends(get_current_user)):
    if current_user.onboarding_completed_at is not None:
        return onboarding_status(current_user)
    service = get_github_app(request)
    try:
        _, repositories = await service.discovery(current_user.id)
    except GitHubAppError as exc:
        raise discovery_error(exc) from None
    if not repositories:
        raise HTTPException(409, {'outcome': 'no_eligible_repositories',
                                  'message': 'At least one accessible public repository is required'})
    return onboarding_status(service.store.complete_onboarding(current_user.id))


@router.post('/onboarding/installation/verify')
async def retry_installation_return(request: Request, current_user: UserModel = Depends(get_current_user)):
    result = installation_return(request, current_user.id)
    if not result or not result.verified_return:
        raise HTTPException(409, 'No verified installation return; restart installation')
    user = await verify_installation_return(get_github_app(request), current_user.id, result)
    return {**onboarding_status(user), 'installation_outcome': result.outcome}
