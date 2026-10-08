from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any

import httpx
import jwt
from sqlalchemy import select

from ai_services.ingestion.persistence.models import (
    GitHubConnectionModel, GitHubInstallationModel, InstallationRepositoryModel,
    RepositoryModel, ConversationScopeModel, utc_now,
)
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.ingestion.sources.interface import RepositoryMetadata
from .settings import GitHubAppSettings


class GitHubAppError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


class GitHubAppService:
    """Single-worker service. User access and installation indexing are distinct."""
    def __init__(self, store, settings: GitHubAppSettings, client: httpx.AsyncClient):
        self.store, self.settings, self.client = store, settings, client
        self.tokens: dict[tuple, tuple[str, datetime]] = {}
        self.generations: dict[str, int] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': self.settings.app_id},
                          self.settings.private_key, algorithm='RS256')

    async def request(self, method: str, path: str, token: str, **kwargs) -> dict | list:
        try:
            response = await self.client.request(method, 'https://api.github.com' + path,
                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}, **kwargs)
        except httpx.HTTPError:
            raise GitHubAppError(502, 'Unable to reach GitHub') from None
        if response.status_code not in {200, 201}:
            code = response.status_code if response.status_code in {401, 403, 404} else 502
            raise GitHubAppError(code, 'GitHub access denied or unavailable; reconnect if authorization expired')
        try:
            result = response.json()
        except ValueError:
            raise GitHubAppError(502, 'Invalid GitHub response') from None
        if not isinstance(result, (dict, list)):
            raise GitHubAppError(502, 'Invalid GitHub response shape')
        return result

    async def pages(self, path: str, token: str, field: str | None = None) -> list[dict]:
        output = []
        page = 1
        while True:
            result = await self.request('GET', path, token, params={'per_page': 100, 'page': page})
            values = result.get(field) if field and isinstance(result, dict) else result
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise GitHubAppError(502, 'Invalid GitHub list response')
            output.extend(values)
            if len(values) < 100:
                return output
            page += 1

    def revoke_user(self, github_id: str) -> None:
        with self.store.session_factory() as session:
            connection = session.scalars(select(GitHubConnectionModel).where(
                GitHubConnectionModel.github_user_id == str(github_id))).first()
            if connection:
                connection.access_status = 'RECONNECT_REQUIRED'
                connection.access_token = ''
                session.commit()

    def user_token(self, user_id: str) -> str:
        connection = self.store.get_user_github_connection(user_id)
        try:
            credential = self.store.get_credential_for_user(user_id)
        except Exception:
            credential = None
        if not credential:
            if connection:
                self.revoke_user(connection.github_user_id)
            raise GitHubAppError(401, 'GitHub reconnect required')
        return credential.token

    async def user_request_pages(self, user_id: str, path: str, field: str) -> list[dict]:
        try:
            return await self.pages(path, self.user_token(user_id), field)
        except GitHubAppError as exc:
            if exc.status_code == 401:
                connection = self.store.get_user_github_connection(user_id)
                if connection:
                    self.revoke_user(connection.github_user_id)
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
            if cached and cached[1] > datetime.now(timezone.utc) + timedelta(seconds=60):
                return GitHubCredential(cached[0])
            body: dict[str, Any] = {'permissions': permissions}
            if repository_ids:
                body['repository_ids'] = [int(value) for value in repository_ids]
            result = await self.request('POST', f'/app/installations/{installation_id}/access_tokens',
                                        self.app_jwt(), json=body)
            if not isinstance(result, dict) or not isinstance(result.get('token'), str) or not result.get('expires_at'):
                raise GitHubAppError(502, 'Invalid installation token response')
            expires = parse_time(result['expires_at'])
            if expires <= datetime.now(timezone.utc) + timedelta(seconds=60):
                raise GitHubAppError(502, 'Installation token already expired')
            if self.generations.get(str(installation_id), 0) != generation:
                raise GitHubAppError(403, 'Installation access changed while issuing credentials')
            self.tokens[key] = (result['token'], expires)
            return GitHubCredential(result['token'])

    def persist_inventory(self, installation: dict, repositories: list[dict], status: str):
        iid = str(installation['id'])
        with self.store.session_factory() as session:
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
            current, inventory = await self.reconcile(iid)
            if not current.get('account') or current.get('suspended_at'):
                continue
            accessible = await self.user_request_pages(user_id, f'/user/installations/{iid}/repositories', 'repositories')
            granted = {str(item['id']) for item in inventory if not item.get('private', True)}
            public = [dict(item, installation_id=iid) for item in accessible
                      if not item.get('private', True) and str(item['id']) in granted]
            summaries.append({'id': iid, 'account_login': current['account']['login'],
                              'account_type': current['account']['type'], 'status': 'ACTIVE',
                              'repository_selection': current['repository_selection'],
                              'public_repository_count': len(public)})
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

    def bind_repository(self, item: dict, user_id: str, branch: str | None = None):
        connection = self.store.get_user_github_connection(user_id)
        repo = self.store.save_repository(RepositoryMetadata(
            github_repository_id=str(item['id']), owner=item['owner']['login'], name=item['name'],
            full_name=item['full_name'], default_branch=item.get('default_branch') or 'main',
            tracked_branch=branch or item.get('default_branch') or 'main',
            repository_url=item['html_url'], visibility='public'), user_id=user_id, connection_id=connection.id)
        with self.store.session_factory() as session:
            record = session.get(RepositoryModel, repo.id)
            record.installation_id, record.access_state = item['installation_id'], 'ACTIVE'
            session.commit()
        return self.store.get_repository(repo.id)

    async def authorize_tracked(self, user_id: str, identifier: str):
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

    def bind_conversation(self, user_id: str, repository_id: str, conversation_id: str):
        if len(conversation_id) > 255:
            raise GitHubAppError(400, 'Conversation ID is too long')
        with self.store.session_factory() as session:
            scope = session.get(ConversationScopeModel, (user_id, conversation_id))
            if scope and scope.repository_id != repository_id:
                raise GitHubAppError(409, 'Conversation belongs to a different repository')
            if not scope:
                session.add(ConversationScopeModel(user_id=user_id, conversation_id=conversation_id,
                                                   repository_id=repository_id))
                session.commit()
