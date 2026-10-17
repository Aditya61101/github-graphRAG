from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import asyncio
import secrets
import time
from urllib.parse import urlencode
from urllib.parse import urlsplit
import json

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select

from ai_services.github_app.service import GitHubAppError, as_utc, oauth_credentials
from ai_services.github_app.auth_state import AuthStateStore, InvalidAuthState
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
    payload: dict = field(default_factory=dict)


@dataclass
class InstallationReturn:
    browser: str
    expires_at: float
    outcome: str
    installation_id: str | None = None
    verified_return: bool = False
    attempt: str | None = None
    github_user_id: str | None = None


class GitHubOAuth:
    """Shared one-use transactions; the signed cookie carries only browser binding."""
    def __init__(self, settings, sessions):
        self.settings = settings
        self.states = AuthStateStore(sessions, settings.encryption_key)
        oauth = OAuth()
        oauth.register(name='github', client_id=settings.client_id, client_secret=settings.client_secret,
            access_token_url='https://github.com/login/oauth/access_token',
            authorize_url='https://github.com/login/oauth/authorize',
            client_kwargs={'code_challenge_method': 'S256'},
            access_token_params={'client_id': settings.client_id, 'client_secret': settings.client_secret},
            token_endpoint_auth_method='client_secret_post')
        self.client = oauth.github

    def create(self, request, kind, user_id=None, github_user_id=None, payload=None):
        browser = request.session.get('oauth_browser') or secrets.token_urlsafe(32)
        # Refresh the signed cookie's timestamp even when reusing browser binding;
        # otherwise a new state could outlive an older, nearly expired cookie.
        request.session['oauth_browser'] = browser
        state = secrets.token_urlsafe(32)
        self.states.put('oauth', state, {'kind': kind, **(payload or {})},
                        user_id=user_id, github_user_id=github_user_id, browser=browser)
        return state

    def consume(self, request):
        state = request.query_params.get('state')
        try:
            row, payload = self.states.consume('oauth', state, browser=request.session.get('oauth_browser'))
        except InvalidAuthState as exc:
            raise HTTPException(400, str(exc)) from None
        if row.user_id and not self.states.current_installation(row.user_id, row.github_user_id, payload.get('attempt')):
            raise HTTPException(400, 'Installation attempt changed; restart installation')
        return OAuthTransaction(request.session['oauth_browser'], row.expires_at,
                                payload['kind'], row.user_id, row.github_user_id, payload)

    async def authorize(self, request, kind='login', user_id=None, github_user_id=None, payload=None):
        verifier = secrets.token_urlsafe(64)
        state = self.create(request, kind, user_id, github_user_id,
            {**(payload or {}), 'oauth': {'redirect_uri': self.settings.callback_url, 'code_verifier': verifier}})
        data = await self.client.create_authorization_url(self.settings.callback_url, state=state, code_verifier=verifier)
        return RedirectResponse(data['url'], status_code=302, headers=PRIVATE_HEADERS)

    def save_return(self, user_id, result):
        self.states.save_installation_return(user_id, result.github_user_id, asdict(result))


PRIVATE_HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer'}


def frontend_origin(settings):
    parsed = urlsplit(settings.frontend_url)
    return f'{parsed.scheme}://{parsed.netloc}'


def get_oauth(request):
    value = getattr(request.app.state, 'github_oauth', None)
    if value is None:
        raise HTTPException(503, 'GitHub OAuth is not initialized')
    return value


@router.get('/github/login')
async def github_login(request: Request):
    flow = get_oauth(request)
    canonical = urlsplit(flow.settings.callback_url)
    if canonical.scheme == 'https' and (request.url.hostname, request.url.port) != (canonical.hostname, canonical.port):
        # A Cloud Run service can have multiple host aliases. Establish the
        # host-only cookie on the callback host, never the incoming alias.
        return RedirectResponse(f'{canonical.scheme}://{canonical.netloc}/auth/github/login',
                                status_code=303, headers=PRIVATE_HEADERS)
    return await flow.authorize(request)


@router.post('/github/install')
async def installation_start(request: Request, current_user: UserModel = Depends(get_current_user)):
    flow = get_oauth(request)
    if request.headers.get('X-DecisionGuard-Auth-Flow') != 'post-handoff-v1':
        raise HTTPException(409, {'code': 'frontend_upgrade_required',
            'message': 'Installation requires the updated secure POST handoff flow.'})
    connection = get_github_app(request).store.get_user_github_connection(current_user.id)
    if not connection or connection.credential_kind != 'github_app':
        raise HTTPException(409, 'GitHub reconnect required')
    handoff, _ = flow.states.start_installation(current_user.id, connection.github_user_id)
    from fastapi.responses import JSONResponse
    origin = urlsplit(flow.settings.callback_url)
    return JSONResponse({'flow': 'post-handoff-v1', 'method': 'POST',
        'action': f'{origin.scheme}://{origin.netloc}/auth/github/install/continue',
        'fields': {'handoff': handoff}, 'expires_in': 120}, headers=PRIVATE_HEADERS)


@router.post('/github/install/continue')
async def installation_continue(request: Request):
    flow = get_oauth(request)
    # This is a top-level form POST from the exact website origin. No API JWT
    # or handoff proof appears in a URL, and no cross-site cookie is required.
    if request.headers.get('origin') != frontend_origin(flow.settings):
        raise HTTPException(403, 'Installation must start from the DecisionGuard website')
    if len(await request.body()) > 2048:
        raise HTTPException(400, 'Invalid installation handoff')
    form = await request.form()
    try:
        row, payload = flow.states.consume('handoff', form.get('handoff'))
    except InvalidAuthState as exc:
        raise HTTPException(400, str(exc)) from None
    service = get_github_app(request)
    connection = service.store.get_user_github_connection(row.user_id)
    if (not connection or connection.credential_kind != 'github_app'
            or connection.github_user_id != row.github_user_id
            or not flow.states.current_installation(row.user_id, row.github_user_id, payload['attempt'])):
        raise HTTPException(403, 'Installation account changed; reconnect and restart')
    # The top-level response can establish a Lax cookie even when third-party
    # cookies are blocked. Automatic GitHub install authorization has no PKCE.
    state = flow.create(request, 'installation', row.user_id, row.github_user_id,
                        {'attempt': payload['attempt'], 'oauth': {'redirect_uri': flow.settings.callback_url}})
    return RedirectResponse(f'https://github.com/apps/{flow.settings.slug}/installations/new?' +
                            urlencode({'state': state}), status_code=303, headers=PRIVATE_HEADERS)


def onboarding_status(user):
    completed = user.onboarding_completed_at is not None
    return {'onboarding_completed': completed,
            'onboarding_completed_at': user.to_dict()['onboarding_completed_at'],
            'next': '/projects' if completed else '/install'}


def installation_return(request, user_id):
    flow = get_oauth(request)
    found = flow.states.get('installation_return', user_id)
    if found:
        row, payload = found
        connection = get_github_app(request).store.get_user_github_connection(user_id)
        if (connection and connection.github_user_id == row.github_user_id
                and flow.states.current_installation(user_id, row.github_user_id, payload['attempt'])):
            return InstallationReturn(**payload)
    return None


def callback_redirect(flow, user, outcome=None):
    # Tokens are delivered only in an exact-origin window message, never a URL.
    result = {'type': 'decisionguard.auth', 'token': create_access_token({'sub': user.id}),
              'next': onboarding_status(user)['next'], 'installation_outcome': outcome}
    encoded = json.dumps(result).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    target = json.dumps(frontend_origin(flow.settings))
    nonce = secrets.token_urlsafe(24)
    headers = {**PRIVATE_HEADERS, 'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'"}
    return HTMLResponse(f'''<!doctype html><meta charset="utf-8"><title>DecisionGuard authentication</title>
<p>Authentication completed. If this window stays open, return to the updated DecisionGuard website and sign in again using its popup flow.</p>
<script nonce="{nonce}">
const result = {encoded};
if (window.opener) {{ window.opener.postMessage(result, {target}); window.close(); }}
</script>''', headers=headers)


async def verify_installation_return(service, user_id, result, flow):
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
        user = flow.states.complete_installation(user_id, result.github_user_id, result.attempt)
        result.outcome = 'success'
        return user
    return service.store.get_user(user_id)


def installation_redirect(flow, user, result):
    try:
        flow.save_return(user.id, result)
    except InvalidAuthState as exc:
        raise HTTPException(400, str(exc)) from None
    return callback_redirect(flow, user, result.outcome)


@router.get('/github/callback')
async def github_callback(request: Request):
    flow = get_oauth(request)
    # A direct GitHub visit is untrusted input; ignore its code/installation hint
    # and start a new protected login. A supplied invalid state is never bypassed.
    if not request.query_params.get('state') and request.query_params.get('code'):
        return await github_login(request)
    txn = flow.consume(request)
    error, code = request.query_params.get('error'), request.query_params.get('code')
    service = get_github_app(request)
    installing = txn.kind in {'installation', 'installation_authorize'}
    result = None
    if installing:
        user = service.store.get_user(txn.user_id)
        connection = service.store.get_user_github_connection(txn.user_id)
        if not user or not connection or connection.github_user_id != txn.github_user_id:
            raise HTTPException(403, 'Installation account binding changed; restart installation')
        hint = (txn.payload.get('installation_id') if txn.kind == 'installation_authorize'
                else request.query_params.get('installation_id'))
        action = 'install' if txn.kind == 'installation_authorize' else request.query_params.get('setup_action')
        result = InstallationReturn(txn.browser, time.time() + 600, 'cancelled', hint,
            attempt=txn.payload['attempt'], github_user_id=txn.github_user_id)
        if error:
            result.outcome = ('cancelled' if error in {'access_denied', 'user_denied'} else
                              'temporary_failure' if error in {'server_error', 'temporarily_unavailable'} else
                              'authorization_failed')
            return installation_redirect(flow, user, result)
        if action == 'request':
            result.outcome = 'pending_approval'
            return installation_redirect(flow, user, result)
        if not hint or not hint.isdigit() or action not in {'install', 'update'}:
            return installation_redirect(flow, user, result)
        if not code and txn.kind == 'installation':
            # Configuration/setup returns do not prove the current GitHub user.
            # Fresh PKCE OAuth preserves the original account/attempt binding.
            return await flow.authorize(request, 'installation_authorize', txn.user_id, txn.github_user_id,
                {'attempt': txn.payload['attempt'], 'installation_id': hint})
    if error:
        raise HTTPException(400, 'GitHub authorization was cancelled or denied; restart login')
    if not code:
        raise HTTPException(400, 'Missing GitHub authorization code; restart login')
    try:
        issued_at = datetime.now(timezone.utc)
        # Restore Authlib's PKCE/redirect data from encrypted shared storage only
        # after atomic browser-bound consumption. No process-local session marker.
        request.session['_state_github_' + request.query_params['state']] = {
            'data': txn.payload['oauth'], 'exp': txn.expires_at}
        token = await flow.client.authorize_access_token(request)
        access, refresh, expiry, refresh_expiry = oauth_credentials(token, issued_at)
        identity = await service.request('GET', '/user', access)
        if not isinstance(identity, dict) or type(identity.get('id')) is not int or identity['id'] < 1 or not identity.get('login'):
            raise HTTPException(502, 'Invalid GitHub identity response')
        if installing:
            existing = service.store.get_user_github_connection(txn.user_id)
            if (not existing or existing.github_user_id != str(identity['id'])
                    or existing.github_user_id != txn.github_user_id):
                raise HTTPException(403, 'Installation authorization must use the initiating GitHub account')
        else:
            with service.store.session_factory() as session:
                existing = session.scalar(select(GitHubConnectionModel).where(
                    GitHubConnectionModel.github_user_id == str(identity['id'])))
        def save_login(lease_owner=None):
            return service.store.upsert_github_login(
                github_user_id=str(identity['id']), username=identity['login'], email=identity.get('email'),
                avatar_url=identity.get('avatar_url'), access_token=access, expires_at=expiry,
                refresh_token=refresh, refresh_expires_at=refresh_expiry, lease_owner=lease_owner)
        if existing:
            async with service.credential_lock(existing.user_id) as owner:
                user, _ = save_login(owner)
        else:
            user, _ = save_login()
        if installing:
            result.verified_return = True
            user = await verify_installation_return(service, user.id, result, flow)
    except InvalidAuthState as exc:
        raise HTTPException(400, str(exc)) from None
    except GitHubAppError as exc:
        if installing:
            result.outcome = discovery_error(exc).detail['outcome']
            return installation_redirect(flow, service.store.get_user(txn.user_id), result)
        raise HTTPException(exc.status_code, str(exc)) from None
    except HTTPException:
        raise
    except httpx.HTTPError:
        if installing:
            result.outcome = 'temporary_failure'
            return installation_redirect(flow, service.store.get_user(txn.user_id), result)
        raise HTTPException(503, 'GitHub authorization temporarily unavailable; restart login') from None
    except Exception:
        if installing:
            result.outcome = 'authorization_failed'
            return installation_redirect(flow, service.store.get_user(txn.user_id), result)
        raise HTTPException(400, 'GitHub authorization failed; restart login') from None
    return installation_redirect(flow, user, result) if installing else callback_redirect(flow, user)

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
    flow = get_oauth(request)
    try:
        user = await verify_installation_return(get_github_app(request), current_user.id, result, flow)
        flow.save_return(user.id, result)
    except InvalidAuthState as exc:
        raise HTTPException(409, str(exc)) from None
    return {**onboarding_status(user), 'installation_outcome': result.outcome}
