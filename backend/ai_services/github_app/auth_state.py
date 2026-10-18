"""Shared expiring state. Hash lookup secrets, encrypt payloads, consume atomically."""
import hashlib
import json
import secrets
import time
from sqlalchemy import delete, select, text
from cryptography.fernet import Fernet
from ai_services.ingestion.persistence.models import GitHubAuthState, GitHubConnectionModel, UserModel, utc_now
from ai_services.ingestion.persistence.concurrency import serialize_metadata_write


class InvalidAuthState(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AuthStateStore:
    def __init__(self, sessions, encryption_key, *, clock=time.time):
        self.sessions, self.cipher, self.clock = sessions, Fernet(encryption_key.encode()), clock

    def now(self, s):
        if self.clock is time.time and s.get_bind().dialect.name == 'postgresql':
            return int(s.scalar(text('SELECT extract(epoch FROM clock_timestamp())')))
        return int(self.clock())

    @staticmethod
    def key(purpose, value):
        return fingerprint(purpose + ':' + value)

    def encode(self, payload):
        return self.cipher.encrypt(json.dumps(payload).encode()).decode()

    def decode(self, row):
        return json.loads(self.cipher.decrypt(row.payload.encode()))

    def _cleanup(self, s, now):
        expired = select(GitHubAuthState.key).where(GitHubAuthState.expires_at <= now).limit(200)
        s.execute(delete(GitHubAuthState).where(GitHubAuthState.key.in_(expired)))

    def put(self, purpose, value, payload, *, ttl=600, user_id=None, github_user_id=None, browser=None, credential_owner=None):
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            now = self.now(s)
            if credential_owner:
                self.require_credentials(s, user_id, credential_owner, self.cipher, now=now)
            self._cleanup(s, now)
            key = self.key(purpose, value)
            row = s.get(GitHubAuthState, key)
            if row is None:
                row = GitHubAuthState(key=key, purpose=purpose)
                s.add(row)
            row.user_id, row.github_user_id = user_id, github_user_id
            row.browser_hash = fingerprint(browser) if browser else None
            row.payload, row.expires_at, row.consumed_at = self.encode(payload), now + ttl, None

    def get(self, purpose, value):
        with self.sessions() as s:
            row = s.get(GitHubAuthState, self.key(purpose, value))
            if not row or row.expires_at <= self.now(s) or row.consumed_at is not None:
                return None
            return row, self.decode(row)

    def consume(self, purpose, value, *, browser=None):
        if not isinstance(value, str) or not 20 <= len(value) <= 256:
            raise InvalidAuthState('Invalid, expired, or replayed authentication state; restart the flow')
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            row = s.get(GitHubAuthState, self.key(purpose, value))
            now = self.now(s)
            if (not row or row.expires_at <= now or row.consumed_at is not None
                    or (row.browser_hash and (not browser or not secrets.compare_digest(row.browser_hash, fingerprint(browser))))):
                raise InvalidAuthState('Invalid, expired, or replayed authentication state; restart the flow')
            payload = self.decode(row)
            row.consumed_at = now
            return row, payload

    def remove(self, purpose, value):
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            s.execute(delete(GitHubAuthState).where(GitHubAuthState.key == self.key(purpose, value)))

    def start_installation(self, user_id, github_user_id):
        attempt, handoff = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        # Serialize supersession with callback completion. No network while locked.
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            now = self.now(s)
            self._cleanup(s, now)
            s.execute(delete(GitHubAuthState).where(GitHubAuthState.user_id == user_id,
                GitHubAuthState.purpose.in_(['handoff', 'installation_return', 'installation_latest'])))
            for purpose, value, payload, ttl in (
                ('installation_latest', user_id, {'attempt': attempt}, 1200),
                ('handoff', handoff, {'attempt': attempt}, 120),
            ):
                s.add(GitHubAuthState(key=self.key(purpose, value), purpose=purpose,
                    user_id=user_id, github_user_id=github_user_id, payload=self.encode(payload),
                    expires_at=now + ttl))
        return handoff, attempt

    def current_installation(self, user_id, github_user_id, attempt):
        found = self.get('installation_latest', user_id)
        return bool(found and found[0].github_user_id == github_user_id and found[1]['attempt'] == attempt)

    def save_installation_return(self, user_id, github_user_id, payload):
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            now = self.now(s)
            latest = s.get(GitHubAuthState, self.key('installation_latest', user_id))
            if (not latest or latest.expires_at <= now or latest.github_user_id != github_user_id
                    or self.decode(latest)['attempt'] != payload['attempt']):
                raise InvalidAuthState('Installation attempt changed; restart installation')
            key = self.key('installation_return', user_id)
            row = s.get(GitHubAuthState, key)
            if row is None:
                row = GitHubAuthState(key=key, purpose='installation_return', user_id=user_id)
                s.add(row)
            row.github_user_id, row.payload = github_user_id, self.encode(payload)
            row.expires_at = min(now + 600, int(payload['expires_at']))

    def complete_installation(self, user_id, github_user_id, attempt):
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            row = s.get(GitHubAuthState, self.key('installation_latest', user_id))
            connection = s.scalar(select(GitHubConnectionModel).where(
                GitHubConnectionModel.user_id == user_id).order_by(GitHubConnectionModel.updated_at.desc()).limit(1))
            if (not row or row.expires_at <= self.now(s) or self.decode(row)['attempt'] != attempt
                    or row.github_user_id != github_user_id or not connection
                    or connection.github_user_id != github_user_id):
                raise InvalidAuthState('Installation attempt changed; restart installation')
            user = s.get(UserModel, user_id)
            if user.onboarding_completed_at is None:
                user.onboarding_completed_at = utc_now()
            return user

    def acquire_credentials(self, user_id):
        owner = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            now, key = self.now(s), self.key('credential_lock', user_id)
            row = s.get(GitHubAuthState, key)
            if row and row.expires_at > now:
                raise InvalidAuthState('GitHub credentials are being updated; retry shortly')
            if row is None:
                row = GitHubAuthState(key=key, purpose='credential_lock', user_id=user_id)
                s.add(row)
            row.payload, row.expires_at = self.encode({'owner': owner}), now + 120
        return owner

    @staticmethod
    def require_credentials(s, user_id, owner, cipher, *, now=None):
        if now is None:
            now = (int(s.scalar(text('SELECT extract(epoch FROM clock_timestamp())')))
                   if s.get_bind().dialect.name == 'postgresql' else int(time.time()))
        row = s.get(GitHubAuthState, AuthStateStore.key('credential_lock', user_id))
        if (not row or row.expires_at <= now or not secrets.compare_digest(
                json.loads(cipher.decrypt(row.payload.encode()))['owner'], owner)):
            raise InvalidAuthState('GitHub credential ownership expired; reconnect or retry shortly')

    def release_credentials(self, user_id, owner):
        with self.sessions.begin() as s:
            serialize_metadata_write(s)
            row = s.get(GitHubAuthState, self.key('credential_lock', user_id))
            if row and secrets.compare_digest(self.decode(row)['owner'], owner):
                s.delete(row)
