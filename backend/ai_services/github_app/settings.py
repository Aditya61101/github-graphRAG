from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


@dataclass(frozen=True)
class GitHubAppSettings:
    app_id: str
    slug: str
    client_id: str
    client_secret: str = field(repr=False)
    private_key: bytes = field(repr=False)
    webhook_secret: str = field(repr=False)
    callback_url: str
    frontend_url: str
    session_secret: str = field(repr=False)
    encryption_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> GitHubAppSettings:
        names = ['GITHUB_APP_ID', 'GITHUB_APP_SLUG', 'GITHUB_APP_CLIENT_ID',
                 'GITHUB_APP_CLIENT_SECRET', 'GITHUB_APP_PRIVATE_KEY_PATH',
                 'GITHUB_APP_WEBHOOK_SECRET', 'GITHUB_APP_CALLBACK_URL', 'FRONTEND_URL',
                 'OAUTH_SESSION_SECRET', 'GITHUB_TOKEN_ENCRYPTION_KEY']
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            raise ValueError('Missing GitHub App configuration: ' + ', '.join(missing))
        values = [os.environ[name] for name in names]
        app_id, slug, client_id, secret, key_path, webhook, callback, frontend, session, encryption = values
        if not app_id.isdigit() or int(app_id) < 1:
            raise ValueError('GITHUB_APP_ID must be a positive numeric App ID')
        if not slug.replace('-', '').isalnum():
            raise ValueError('GITHUB_APP_SLUG must be a GitHub App slug')
        for name, value in [('GITHUB_APP_CALLBACK_URL', callback), ('FRONTEND_URL', frontend)]:
            parsed = urlparse(value)
            if parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError(f'{name} must be an absolute URL without query or fragment')
            if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1'}:
                raise ValueError(f'{name} requires HTTPS outside localhost')
        if len(session) < 32:
            raise ValueError('OAUTH_SESSION_SECRET must contain at least 32 characters')
        try:
            Fernet(encryption.encode())
        except Exception:
            raise ValueError('GITHUB_TOKEN_ENCRYPTION_KEY must be a Fernet key') from None
        try:
            key = Path(key_path).read_bytes()
            parsed_key = load_pem_private_key(key, password=None)
            if not isinstance(parsed_key, RSAPrivateKey):
                raise ValueError()
        except Exception:
            raise ValueError('GITHUB_APP_PRIVATE_KEY_PATH must contain a readable unencrypted RSA PEM key') from None
        return cls(app_id, slug, client_id, secret, key, webhook, callback, frontend.rstrip('/'), session, encryption)
