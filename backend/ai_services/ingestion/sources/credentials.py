from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GitHubCredential:
    """Represents an authenticated user's GitHub credential."""

    token: str
    token_type: str = "Bearer"

    def __repr__(self) -> str:
        # Never expose token string in representations or logs
        return "GitHubCredential(token=***REDACTED***)"

    def __str__(self) -> str:
        return "GitHubCredential(token=***REDACTED***)"


def sanitize_sensitive_text(text: str) -> str:
    """Strip or redact sensitive token headers or credentials from text/logs/errors."""
    if not text:
        return ""
    # Redact Bearer / token strings
    scrubbed = re.sub(r"(?i)Authorization:\s*(Bearer|token)\s+[^\s'\"]+", "Authorization: [REDACTED]", text)
    scrubbed = re.sub(r"gh[pousr]_[A-Za-z0-9_]{36,255}", "[REDACTED_GITHUB_TOKEN]", scrubbed)
    return scrubbed


class CredentialProvider(Protocol):
    """Abstraction for retrieving user-scoped credentials.

    Enables pluggable credential backends (SQLite, encrypted DB, AWS Secrets Manager, Vault).
    """

    def get_credential(
        self,
        connection_id: str | None = None,
        user_id: str | None = None,
    ) -> GitHubCredential | None:
        """Resolve a GitHubCredential for the specified connection or user."""
        ...
