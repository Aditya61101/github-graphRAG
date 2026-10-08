from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import time
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.models import UserModel
from api_services.app.utils.github_app_access import get_github_app
from api_services.app.utils.jwt_utils import create_access_token, get_current_user

router = APIRouter()


@dataclass(frozen=True)
class OAuthTransaction:
    browser: str
    expires_at: float
    kind: str
    user_id: str | None = None


class GitHubOAuth:
    """Temporary one-use transactions, not application JWT sessions."""
    def __init__(self, settings):
        self.settings = settings
        self.transactions: dict[str, OAuthTransaction] = {}
        self.consumed: dict[str, float] = {}
        oauth = OAuth()
        oauth.register(name='github', client_id=settings.client_id, client_secret=settings.client_secret,
            access_token_url='https://github.com/login/oauth/access_token',
            authorize_url='https://github.com/login/oauth/authorize',
            client_kwargs={'code_challenge_method': 'S256'},
            access_token_params={'client_id': settings.client_id, 'client_secret': settings.client_secret},
            token_endpoint_auth_method='client_secret_post')
        self.client = oauth.github

    def create(self, request, kind, user_id=None):
        self.consumed = {key: value for key, value in self.consumed.items() if value > time.time()}
        for key, value in list(self.transactions.items()):
            if value.expires_at < time.time():
                del self.transactions[key]
        browser = request.session.setdefault('oauth_browser', secrets.token_urlsafe(32))
        state = secrets.token_urlsafe(32)
        self.transactions[state] = OAuthTransaction(browser, time.time() + 600, kind, user_id)
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
    state = flow.create(request, 'installation', current_user.id)
    # Automatic post-install authorization has no explicit-login PKCE challenge.
    # Register Authlib state and enforce the browser-bound one-use transaction.
    await flow.client.save_authorize_data(request, state=state, redirect_uri=flow.settings.callback_url)
    return {'installation_url': f'https://github.com/apps/{flow.settings.slug}/installations/new?' + urlencode({'state': state})}


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
    if request.query_params.get('error'):
        raise HTTPException(400, 'GitHub authorization was cancelled or denied')
    if not request.query_params.get('code'):
        raise HTTPException(400, 'Missing GitHub authorization code')
    app_service = get_github_app(request)
    try:
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
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(token['expires_in']))
        if expiry <= datetime.now(timezone.utc):
            raise HTTPException(400, 'GitHub user token already expired')
        user, _ = app_service.store.upsert_github_login(
            github_user_id=str(identity['id']), username=identity['login'], email=identity.get('email'),
            avatar_url=identity.get('avatar_url'), access_token=token['access_token'], expires_at=expiry)
        summaries, _ = await app_service.discovery(user.id)
        hint = request.query_params.get('installation_id')
        if txn.kind == 'installation' and hint and not any(item['id'] == hint for item in summaries):
            raise HTTPException(403, 'Installation is inaccessible or awaiting approval')
        usable = any(item['public_repository_count'] > 0 for item in summaries)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, 'GitHub authorization failed; restart login') from None
    application_token = create_access_token({'sub': user.id})
    # Existing prototype delivery; never log this URL.
    params = urlencode({'token': application_token, 'next': '/dashboard' if usable else '/install'})
    return RedirectResponse(flow.settings.frontend_url + '/auth/success?' + params)


@router.get('/me')
async def me(request: Request, current_user: UserModel = Depends(get_current_user)):
    service = get_github_app(request)
    try:
        installations, _ = await service.discovery(current_user.id)
        reconnect = False
    except GitHubAppError as exc:
        if exc.status_code != 401:
            raise HTTPException(exc.status_code, str(exc)) from None
        installations, reconnect = [], True
    return {'user': current_user.to_dict(), 'installations': installations,
            'installation_onboarding_required': reconnect or not any(i['public_repository_count'] for i in installations),
            'reconnect_required': reconnect}
