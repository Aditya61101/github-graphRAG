from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from cryptography.fernet import InvalidToken
from sqlalchemy import select

from ai_services.ingestion.persistence.concurrency import serialize_metadata_write
from ai_services.ingestion.persistence.models import (
    ConversationScopeModel,
    GitHubConnectionModel,
    GitHubInstallationModel,
    InstallationRepositoryModel,
    RepositoryModel,
    utc_now,
)
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.ingestion.sources.interface import RepositoryMetadata

from .settings import GitHubAppSettings
from .auth_state import AuthStateStore, InvalidAuthState


class GitHubAppError(Exception):
    def __init__(self, status_code: int, message: str, reconnect_required: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.reconnect_required = reconnect_required


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def as_utc(value: datetime) -> datetime:
    # SQLite returns naive UTC values even when aware timestamps were written.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def oauth_credentials(result: dict, issued_at: datetime) -> tuple[str, str, datetime, datetime]:
    """Validate the full rotation before writing anything; never echo provider data."""
    try:
        access, refresh = result['access_token'], result['refresh_token']
        access_seconds, refresh_seconds = int(result['expires_in']), int(result['refresh_token_expires_in'])
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ValueError()
        if access_seconds <= 60 or refresh_seconds <= 0:
            raise ValueError()
        return (access, refresh, issued_at + timedelta(seconds=access_seconds),
                issued_at + timedelta(seconds=refresh_seconds))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise GitHubAppError(502, 'Invalid GitHub OAuth credential response') from None


class GitHubAppService:
    """User access and installation indexing are distinct; rotations are shared."""
    def __init__(self, store, settings: GitHubAppSettings, client: httpx.AsyncClient):
        self.store, self.settings, self.client = store, settings, client
        self.tokens: dict[tuple, tuple[str, datetime]] = {}
        self.generations: dict[str, int] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.user_locks: dict[str, asyncio.Lock] = {}
        # Keep a completed rotation until its transaction succeeds. A database
        # outage must not make the next request spend the old refresh token again.
        self.pending_rotations: dict[str, tuple] = {}
        self.auth_states = AuthStateStore(store.session_factory, settings.encryption_key)

    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': self.settings.app_id},
                          self.settings.private_key, algorithm='RS256')

    async def request(self, method: str, path: str, token: str, *, allow_no_content: bool = False, **kwargs) -> dict | list:
        try:
            response = await self.client.request(method, 'https://api.github.com' + path,
                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}, **kwargs)
        except httpx.HTTPError:
            raise GitHubAppError(502, 'Unable to reach GitHub') from None
        if allow_no_content and response.status_code == 204:
            return {}
        if allow_no_content and response.status_code in {200, 201}:
            raise GitHubAppError(502, 'Invalid GitHub collaborator confirmation')
        if response.status_code not in {200, 201}:
            if (response.status_code == 429 or response.status_code >= 500 or
                    (response.status_code == 403 and (response.headers.get('Retry-After') or
                     response.headers.get('X-RateLimit-Remaining') == '0'))):
                raise GitHubAppError(503, 'GitHub temporarily unavailable or rate limited; retry later')
            code = response.status_code if response.status_code in {401, 403, 404} else 502
            raise GitHubAppError(code, 'GitHub access denied or unavailable')
        try:
            result = response.json()
        except ValueError:
            raise GitHubAppError(502, 'Invalid GitHub response') from None
        if not isinstance(result, (dict, list)):
            raise GitHubAppError(502, 'Invalid GitHub response shape')
        return result

    async def pages(self, path: str, token: str, field: str | None = None, *, params: dict | None = None) -> list[dict]:
        output = []
        page = 1
        while True:
            result = await self.request('GET', path, token, params={**(params or {}), 'per_page': 100, 'page': page})
            values = result.get(field) if field and isinstance(result, dict) else result
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise GitHubAppError(502, 'Invalid GitHub list response')
            output.extend(values)
            if len(values) < 100:
                return output
            page += 1

    def revoke_user(self, github_id: str, expected_token: str | None = None) -> None:
        with self.store.session_factory() as session:
            serialize_metadata_write(session)
            connection = session.scalars(select(GitHubConnectionModel).where(
                GitHubConnectionModel.github_user_id == str(github_id))).first()
            if connection and (expected_token is None or connection.access_token == expected_token):
                connection.access_status = 'RECONNECT_REQUIRED'
                connection.access_token = ''
                connection.refresh_token = None
                session.commit()

    def _reconnect(self, connection):
        if connection:
            self.revoke_user(connection.github_user_id, connection.access_token)
        raise GitHubAppError(401, 'GitHub reconnect required', reconnect_required=True)

    async def user_token(self, user_id: str, rejected_token: str | None = None) -> str:
        async with self.credential_lock(user_id) as owner:
            try:
                return await self._user_token_locked(user_id, rejected_token, owner)
            except InvalidAuthState:
                raise GitHubAppError(503, 'GitHub credential ownership changed; reconnect or retry shortly') from None

    @asynccontextmanager
    async def credential_lock(self, user_id):
        async with self.user_locks.setdefault(user_id, asyncio.Lock()):
            try:
                owner = self.auth_states.acquire_credentials(user_id)
            except InvalidAuthState:
                raise GitHubAppError(503, 'GitHub credentials are being updated; retry shortly') from None
            try:
                # Bound network work below the shared 120-second lease. No
                # transaction or database connection is held during provider IO.
                try:
                    async with asyncio.timeout(90):
                        yield owner
                except TimeoutError:
                    raise GitHubAppError(503, 'GitHub credentials update timed out; reconnect or retry shortly') from None
            finally:
                self.auth_states.release_credentials(user_id, owner)

    async def _user_token_locked(self, user_id: str, rejected_token: str | None, owner=None) -> str:
        connection = self.store.get_user_github_connection(user_id)
        pending = self.pending_rotations.get(user_id)
        shared = self.auth_states.get('rotation', user_id)
        if shared:
            payload = shared[1]
            pending = (SimpleNamespace(id=payload['connection_id'], user_id=user_id,
                access_token=payload['previous_access'], refresh_token=payload['previous_refresh']),
                (payload['access'], payload['refresh'], datetime.fromisoformat(payload['expiry']),
                 datetime.fromisoformat(payload['refresh_expiry'])))
            self.pending_rotations[user_id] = pending
        if pending:
            previous, credentials = pending
            if (connection and connection.access_status == 'ACTIVE' and
                    connection.access_token == previous.access_token and
                    connection.refresh_token == previous.refresh_token):
                self._persist_rotation(user_id, previous, credentials, owner)
                connection = self.store.get_user_github_connection(user_id)
            else:
                # A new login or revocation supersedes any pending rotation.
                self.pending_rotations.pop(user_id, None)
                self.auth_states.remove('rotation', user_id)
        if not connection or connection.credential_kind != 'github_app' or connection.access_status != 'ACTIVE':
            self._reconnect(connection)
        try:
            token = self.store.decrypt_token(connection.access_token)
        except (InvalidToken, RuntimeError):
            # Missing/misconfigured encryption keys are operational failures.
            raise GitHubAppError(503, 'Stored GitHub credentials unavailable') from None
        now = datetime.now(UTC)
        expiry = connection.access_token_expires_at
        if expiry and as_utc(expiry) > now + timedelta(seconds=60) and token != rejected_token:
            return token
        if (not connection.refresh_token or not connection.refresh_token_expires_at or
                as_utc(connection.refresh_token_expires_at) <= now):
            self._reconnect(connection)
        try:
            refresh = self.store.decrypt_token(connection.refresh_token)
        except (InvalidToken, RuntimeError):
            raise GitHubAppError(503, 'Stored GitHub credentials unavailable') from None
        try:
            response = await self.client.post('https://github.com/login/oauth/access_token',
                headers={'Accept': 'application/json'}, data={
                    'client_id': self.settings.client_id, 'client_secret': self.settings.client_secret,
                    'grant_type': 'refresh_token', 'refresh_token': refresh})
        except httpx.HTTPError:
            raise GitHubAppError(503, 'GitHub token refresh unavailable; retry later') from None
        if (response.status_code == 429 or response.status_code >= 500 or
                (response.status_code == 403 and (response.headers.get('Retry-After') or
                 response.headers.get('X-RateLimit-Remaining') == '0'))):
            raise GitHubAppError(503, 'GitHub token refresh unavailable; retry later')
        try:
            result = response.json()
        except ValueError:
            raise GitHubAppError(502, 'Invalid GitHub OAuth response') from None
        if not isinstance(result, dict):
            raise GitHubAppError(502, 'Invalid GitHub OAuth response')
        error = result.get('error')
        if isinstance(error, str) and error in {'bad_refresh_token', 'invalid_grant', 'access_denied'}:
            self._reconnect(connection)
        if response.status_code != 200 or error:
            # Client configuration errors and unknown responses do not revoke users.
            raise GitHubAppError(503, 'GitHub token refresh failed; retry later')
        credentials = oauth_credentials(result, now)
        # Persist provider success before replacing stored credentials, so another
        # instance can finish a failed SQL publication without spending the old
        # rotating refresh token again. Payload is encrypted with the existing key.
        self.auth_states.put('rotation', user_id, {
            'connection_id': connection.id, 'previous_access': connection.access_token,
            'previous_refresh': connection.refresh_token, 'access': credentials[0],
            'refresh': credentials[1], 'expiry': credentials[2].isoformat(),
            'refresh_expiry': credentials[3].isoformat()}, ttl=600, user_id=user_id, credential_owner=owner)
        self.pending_rotations[user_id] = (connection, credentials)
        self._persist_rotation(user_id, connection, credentials, owner)
        return credentials[0]

    def _persist_rotation(self, user_id, connection, credentials, owner=None):
        try:
            self.store.rotate_github_tokens(connection, *credentials, owner)
        except Exception:  # noqa: BLE001 - sanitize storage errors and retain pending rotation
            raise GitHubAppError(503, 'Unable to persist GitHub token refresh; retry later') from None
        self.auth_states.remove('rotation', user_id)
        self.pending_rotations.pop(user_id, None)

    async def user_request_pages(self, user_id: str, path: str, field: str) -> list[dict]:
        token = await self.user_token(user_id)
        try:
            return await self.pages(path, token, field)
        except GitHubAppError as exc:
            if exc.status_code != 401:
                raise
        # Retry a rejected user token once; a concurrent rotation may already
        # have replaced it. Never force another rotation of that new credential.
        token = await self.user_token(user_id, rejected_token=token)
        try:
            return await self.pages(path, token, field)
        except GitHubAppError as exc:
            if exc.status_code == 401:
                connection = self.store.get_user_github_connection(user_id)
                if connection and self.store.decrypt_token(connection.access_token) == token:
                    self._reconnect(connection)
            raise

    def invalidate(self, installation_id: str) -> None:
        self.generations[str(installation_id)] = self.generations.get(str(installation_id), 0) + 1
        for key in list(self.tokens):
            if key[0] == str(installation_id):
                del self.tokens[key]

    async def installation_token(self, installation_id: str, repository_ids: tuple[str, ...] = (),
                                 permissions: dict[str, str] | None = None) -> GitHubCredential:
        permissions = permissions or {'contents': 'read', 'metadata': 'read'}
        key = (str(installation_id), tuple(sorted(repository_ids)), tuple(sorted(permissions.items())))
        lock = self.locks.setdefault('token:' + str(key), asyncio.Lock())
        async with lock:
            generation = self.generations.get(str(installation_id), 0)
            cached = self.tokens.get(key)
            if cached and cached[1] > datetime.now(UTC) + timedelta(seconds=60):
                return GitHubCredential(cached[0])
            body: dict[str, Any] = {'permissions': permissions}
            if repository_ids:
                body['repository_ids'] = [int(value) for value in repository_ids]
            result = await self.request('POST', f'/app/installations/{installation_id}/access_tokens',
                                        self.app_jwt(), json=body)
            if not isinstance(result, dict) or not isinstance(result.get('token'), str) or not result.get('expires_at'):
                raise GitHubAppError(502, 'Invalid installation token response')
            expires = parse_time(result['expires_at'])
            if expires <= datetime.now(UTC) + timedelta(seconds=60):
                raise GitHubAppError(502, 'Installation token already expired')
            if self.generations.get(str(installation_id), 0) != generation:
                raise GitHubAppError(403, 'Installation access changed while issuing credentials')
            self.tokens[key] = (result['token'], expires)
            return GitHubCredential(result['token'])

    def persist_inventory(self, installation: dict, repositories: list[dict], status: str):
        iid = str(installation['id'])
        with self.store.session_factory() as session:
            serialize_metadata_write(session)
            item = session.get(GitHubInstallationModel, iid)
            previous = (item.status, item.repository_selection) if item else None
            if not item:
                item = GitHubInstallationModel(id=iid)
                session.add(item)
            account = installation.get('account') or {}
            item.app_id = str(installation.get('app_id') or self.settings.app_id)
            item.account_id = str(account.get('id') or (item.account_id or 'unknown'))
            item.account_login = account.get('login') or item.account_login or 'unknown'
            item.account_type = account.get('type') or item.account_type or 'unknown'
            item.status = status
            item.repository_selection = installation.get('repository_selection') or item.repository_selection or 'selected'
            item.reconciled_at = item.updated_at = utc_now()
            old = session.scalars(select(InstallationRepositoryModel).where(
                InstallationRepositoryModel.installation_id == iid)).all()
            previous_inventory = {(row.github_repository_id, row.full_name, row.is_private) for row in old if row.granted}
            for row in old:
                row.granted = 0
            for repo in repositories:
                rid = str(repo['id'])
                row = session.get(InstallationRepositoryModel, (iid, rid))
                if not row:
                    row = InstallationRepositoryModel(installation_id=iid, github_repository_id=rid)
                    session.add(row)
                row.full_name, row.is_private = repo['full_name'], int(repo.get('private', True))
                row.granted, row.payload = 1, json.dumps(repo)
            tracked = session.scalars(select(RepositoryModel).where(RepositoryModel.installation_id == iid)).all()
            for repo in tracked:
                visible = next((r for r in repositories if str(r['id']) == repo.github_repository_id), None)
                repo.access_state = 'ACTIVE' if status == 'ACTIVE' and visible and not visible.get('private', True) else status if status != 'ACTIVE' else 'REMOVED'
            current_inventory = {(str(row['id']), row['full_name'], int(row.get('private', True))) for row in repositories}
            if previous != (status, item.repository_selection) or previous_inventory != current_inventory:
                self.invalidate(iid)
            session.commit()

    async def reconcile(self, installation_id: str) -> tuple[dict, list[dict]]:
        iid = str(installation_id)
        if not iid.isdigit():
            raise GitHubAppError(400, 'Invalid installation ID')
        async with self.locks.setdefault('inventory:' + iid, asyncio.Lock()):
            try:
                installation = await self.request('GET', f'/app/installations/{iid}', self.app_jwt())
            except GitHubAppError as exc:
                if exc.status_code == 404:
                    installation = {'id': iid, 'app_id': self.settings.app_id}
                    self.persist_inventory(installation, [], 'DELETED')
                    return installation, []
                raise
            if not isinstance(installation, dict) or str(installation.get('app_id')) != self.settings.app_id:
                raise GitHubAppError(403, 'Installation does not belong to DecisionGuard')
            status = 'SUSPENDED' if installation.get('suspended_at') else 'ACTIVE'
            repositories = []
            if status == 'ACTIVE':
                credential = await self.installation_token(iid, permissions={'metadata': 'read'})
                repositories = await self.pages('/installation/repositories', credential.token, 'repositories')
            self.persist_inventory(installation, repositories, status)
            return installation, repositories

    async def discovery(self, user_id: str) -> tuple[list[dict], list[dict]]:
        installations = await self.user_request_pages(user_id, '/user/installations', 'installations')
        summaries, repositories = [], []
        for hint in installations:
            if str(hint.get('app_id')) != self.settings.app_id:
                continue
            iid = str(hint['id'])
            try:
                current, inventory = await self.reconcile(iid)
            except GitHubAppError as exc:
                if exc.status_code == 401:
                    raise GitHubAppError(502, 'GitHub installation authentication unavailable') from None
                raise
            if not current.get('account') or current.get('suspended_at'):
                continue
            accessible = await self.user_request_pages(user_id, f'/user/installations/{iid}/repositories', 'repositories')
            granted = {str(item['id']) for item in inventory if not item.get('private', True)}
            public = [
                dict(item, installation_id=iid) for item in accessible if not item.get('private', True) and str(item['id']) in granted
            ]
            summaries.append(
                {   
                    'id': iid, 
                    'account_login': current['account']['login'],
                    'account_type': current['account']['type'], 
                    'status': 'ACTIVE',
                    'repository_selection': current['repository_selection'],
                    'public_repository_count': len(public)
                }
            )
            repositories.extend(public)
        return summaries, repositories

    async def select_repository(self, user_id: str, identifier: str, installation_hint: str | None = None) -> dict:
        from ai_services.ingestion.sources.interface import RepositoryRef
        full_name = RepositoryRef.parse(identifier).full_name
        tracked = self.store.get_repository(full_name)
        if tracked and tracked.user_id and tracked.user_id != user_id:
            raise GitHubAppError(409, 'Repository is owned by another DecisionGuard user')
        _, repositories = await self.discovery(user_id)
        matches = [item for item in repositories if item['full_name'].lower() == full_name.lower()
                   and (not installation_hint or item['installation_id'] == str(installation_hint))]
        if not matches:
            raise GitHubAppError(403, 'Repository is not currently granted to this user and App')
        return matches[0]

    def monitoring_capabilities(self, user_id: str, item: dict) -> dict:
        """Use user-token permissions; never infer admin from installation inventory."""
        permissions = item.get('permissions')
        admin = permissions.get('admin') if isinstance(permissions, dict) else None
        admin = admin if type(admin) is bool else None
        tracked = self.store.get_repository(str(item['id']))
        named = self.store.get_repository(item['full_name'])
        records = [record for record in (tracked, named) if record]
        if any(record.user_id and record.user_id != user_id for record in records):
            reason, allowed = 'owned_by_another_user', False
        elif admin is None:
            reason, allowed = 'permission_unknown', None
        elif not admin:
            reason, allowed = 'github_admin_required', False
        elif tracked and tracked.user_id == user_id and tracked.installation_id != item['installation_id']:
            reason, allowed = 'installation_binding_mismatch', False
        else:
            reason, allowed = 'allowed', True
        own = tracked if tracked and tracked.user_id == user_id else None
        status = own.status if own else 'NOT_INDEXED'
        return {
            'github_admin': admin,
            'can_enable_monitoring': allowed if status in {'IDLE', 'NOT_INDEXED'} else False,
            'can_retry_indexing': allowed if status == 'FAILED' else False,
            'reason': reason,
        }

    def require_monitoring_admin(self, user_id: str, item: dict) -> None:
        """Revalidate administrative policy immediately before a manual mutation."""
        capabilities = self.monitoring_capabilities(user_id, item)
        if capabilities['reason'] == 'owned_by_another_user':
            raise GitHubAppError(409, 'Repository is owned by another DecisionGuard user')
        if capabilities['github_admin'] is None:
            raise GitHubAppError(503, 'GitHub repository administration permission could not be verified; retry discovery')
        if capabilities['github_admin'] is not True:
            raise GitHubAppError(403, 'GitHub repository administrator permission is required to enable or retry monitoring')
        # Ingest can rebind an installation after verified selection; sync keeps
        # the stricter authorize_tracked installation binding check.

    def bind_repository(self, item: dict, user_id: str, branch: str | None = None):
        connection = self.store.get_user_github_connection(user_id)
        repo = self.store.save_repository(RepositoryMetadata(
            github_repository_id=str(item['id']), owner=item['owner']['login'], name=item['name'],
            full_name=item['full_name'], default_branch=item.get('default_branch') or 'main',
            tracked_branch=branch or item.get('default_branch') or 'main',
            repository_url=item['html_url'], visibility='public'), user_id=user_id, connection_id=connection.id)
        with self.store.session_factory() as session:
            serialize_metadata_write(session)
            record = session.get(RepositoryModel, repo.id)
            if record.user_id != user_id:
                raise GitHubAppError(409, 'Repository ownership changed during binding')
            installation = session.get(GitHubInstallationModel, str(item['installation_id']))
            granted = session.get(InstallationRepositoryModel,
                                  (str(item['installation_id']), record.github_repository_id))
            if (not installation or installation.status != 'ACTIVE' or not granted
                    or not granted.granted or granted.is_private
                    or granted.full_name.lower() != record.full_name.lower()):
                raise GitHubAppError(409, 'Installation access changed during repository binding')
            record.installation_id, record.access_state = item['installation_id'], 'ACTIVE'
            session.commit()
        return self.store.get_repository(repo.id)

    async def authorize_tracked(self, user_id: str, identifier: str, require_admin: bool = False):
        repo = self.store.get_repository(identifier)
        if not repo:
            raise GitHubAppError(404, 'Repository not found')
        if repo.user_id != user_id:
            raise GitHubAppError(403, 'Repository access denied')
        if not repo.installation_id:
            raise GitHubAppError(403, 'Repository requires GitHub App reconnect and selection')
        selected = await self.select_repository(user_id, repo.full_name, repo.installation_id)
        if str(selected['id']) != repo.github_repository_id:
            raise GitHubAppError(403, 'Repository identity changed')
        if require_admin:
            self.require_monitoring_admin(user_id, selected)
        return self.store.get_repository(repo.id)

    async def indexing_credential(self, repository_id: str, installation_id: str):
        repo = self.store.get_repository(repository_id)
        if not repo or repo.installation_id != str(installation_id):
            raise GitHubAppError(403, 'Repository installation binding is invalid')
        current, inventory = await self.reconcile(installation_id)
        granted = any(str(item['id']) == repo.github_repository_id and not item.get('private', True)
                      for item in inventory)
        if not granted or current.get('suspended_at'):
            raise GitHubAppError(403, 'Installation repository access is inactive')
        return await self.installation_token(str(installation_id), (repo.github_repository_id,))

    async def _read_user_request(self, user_id: str, path: str):
        """Fresh user-authenticated read, using the existing rotation/revocation policy."""
        token = await self.user_token(user_id)
        try:
            return await self.request('GET', path, token)
        except GitHubAppError as exc:
            if exc.status_code != 401:
                raise
        token = await self.user_token(user_id, rejected_token=token)
        try:
            return await self.request('GET', path, token)
        except GitHubAppError as exc:
            if exc.status_code == 401:
                connection = self.store.get_user_github_connection(user_id)
                if connection and self.store.decrypt_token(connection.access_token) == token:
                    self._reconnect(connection)
            raise

    async def authorize_repository_read(self, user_id: str, identifier: str):
        """Shared knowledge reads only; never select, bind, or transfer ownership."""
        repo = self.store.get_repository(identifier)
        if not repo:
            raise GitHubAppError(404, 'Repository not found')
        if repo.user_id == user_id:
            return await self.authorize_tracked(user_id, repo.id)
        if not repo.installation_id:
            raise GitHubAppError(403, 'Repository installation is unavailable')
        identity = await self._read_user_request(user_id, '/user')
        connection = self.store.get_user_github_connection(user_id)
        if (not isinstance(identity, dict) or not connection or
                str(identity.get('id')) != connection.github_user_id or not identity.get('login')):
            raise GitHubAppError(403, 'GitHub user identity could not be verified')
        path = f'/repos/{quote(repo.owner, safe="")}/{quote(repo.name, safe="")}'
        current = await self._read_user_request(user_id, path)
        permissions = current.get('permissions') if isinstance(current, dict) else None
        if (not isinstance(current, dict) or str(current.get('id')) != repo.github_repository_id or
                str(current.get('full_name', '')).lower() != repo.full_name.lower() or
                not isinstance(permissions, dict) or permissions.get('pull') is not True):
            raise GitHubAppError(403, 'Current repository read permission could not be verified')
        installation, inventory = await self.reconcile(repo.installation_id)
        granted = any(str(item['id']) == repo.github_repository_id and
                      str(item.get('full_name', '')).lower() == repo.full_name.lower() and
                      not item.get('private', True) for item in inventory)
        if not granted or not installation.get('account') or installation.get('suspended_at'):
            raise GitHubAppError(403, 'Bound repository installation is inactive')
        credential = await self.installation_token(repo.installation_id, (repo.github_repository_id,),
                                                   permissions={'metadata': 'read'})
        # Public pull permission is insufficient: membership is verified separately
        # with the bound installation, including read-only/team collaborators.
        try:
            await self.request('GET', f'{path}/collaborators/{quote(identity["login"], safe="")}',
                               credential.token, allow_no_content=True)
        except GitHubAppError as exc:
            if exc.status_code == 404:
                raise GitHubAppError(403, 'Verified repository collaborator access is required') from None
            raise
        fresh = self.store.get_repository(repo.id)
        if (not fresh or fresh.installation_id != repo.installation_id or
                fresh.github_repository_id != repo.github_repository_id or fresh.user_id != repo.user_id or
                fresh.access_state != 'ACTIVE'):
            raise GitHubAppError(403, 'Repository access changed during authorization')
        return fresh

    def bind_conversation(self, user_id: str, repository_id: str, conversation_id: str):
        if len(conversation_id) > 255:
            raise GitHubAppError(400, 'Conversation ID is too long')
        with self.store.session_factory() as session:
            serialize_metadata_write(session)
            scope = session.get(ConversationScopeModel, (user_id, conversation_id))
            if scope and scope.repository_id != repository_id:
                raise GitHubAppError(409, 'Conversation belongs to a different repository')
            if not scope:
                session.add(ConversationScopeModel(user_id=user_id, conversation_id=conversation_id,
                                                   repository_id=repository_id))
                session.commit()
