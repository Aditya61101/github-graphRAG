from .interface import (
    CorruptedCloneError,
    RepositoryAuthenticationError,
    RepositoryMetadata,
    RepositoryNotFoundError,
    RepositoryRef,
    RepositorySnapshot,
    RepositorySource,
    RepositorySourceError,
)
from .credentials import CredentialProvider, GitHubCredential, sanitize_sensitive_text
from .github import GitHubRepositorySource
from .classifier import classify_file_for_ingestion

__all__ = [
    "CorruptedCloneError",
    "CredentialProvider",
    "GitHubCredential",
    "RepositoryAuthenticationError",
    "RepositoryMetadata",
    "RepositoryNotFoundError",
    "RepositoryRef",
    "RepositorySnapshot",
    "RepositorySource",
    "RepositorySourceError",
    "GitHubRepositorySource",
    "classify_file_for_ingestion",
    "sanitize_sensitive_text",
]
