from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_services.ingestion.rkg.incremental import ChangeKind, FileChange


from ai_services.ingestion.sources.credentials import GitHubCredential


class RepositorySourceError(Exception):
    """Base exception for repository source operations."""
    pass


class RepositoryNotFoundError(RepositorySourceError):
    """Raised when the specified repository does not exist or is inaccessible."""
    pass


class RepositoryAuthenticationError(RepositorySourceError):
    """Raised when repository credentials or tokens are invalid or lack permissions."""
    pass


class CorruptedCloneError(RepositorySourceError):
    """Raised when an existing local clone is corrupted and cannot be updated."""
    pass


class PrivateRepositoryUnsupportedError(RepositorySourceError):
    """Raised when a repository is private (DecisionGuard prototype supports public repositories only)."""
    pass


class RepositoryOwnershipConflictError(RepositorySourceError):
    """Raised when attempting to ingest or connect a repository already owned by another user."""
    pass


@dataclass(frozen=True)
class RepositoryRef:
    """Canonical pointer to a remote repository and optional branch/commit."""
    owner: str
    name: str
    branch: str | None = None
    commit_sha: str | None = None
    github_repo_id: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @classmethod
    def parse(cls, identifier: str, branch: str | None = None, github_repo_id: str | None = None) -> RepositoryRef:
        """Parse repository identifier into a structured RepositoryRef."""
        raw = identifier.strip()

        # Handle branch in @ notation if not explicitly provided
        if "@" in raw and not branch:
            raw, inline_branch = raw.split("@", 1)
            branch = inline_branch.strip() or None

        # Clean URLs and SSH strings
        raw = re.sub(r"^https?://[^/]+/", "", raw)
        raw = re.sub(r"^git@[^:]+:", "", raw)
        raw = re.sub(r"\.git$", "", raw)
        raw = raw.strip("/ ")

        parts = raw.split("/")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid repository identifier '{identifier}'. Expected format: 'owner/name' or GitHub URL."
            )

        owner = parts[0].strip()
        name = parts[1].strip()

        if not owner or not name:
            raise ValueError(f"Invalid repository owner or name parsed from '{identifier}'.")

        return cls(owner=owner, name=name, branch=branch, github_repo_id=github_repo_id)


@dataclass(frozen=True)
class RepositoryMetadata:
    """Metadata describing a remote repository."""
    github_repository_id: str | int
    owner: str
    name: str
    full_name: str
    default_branch: str
    tracked_branch: str
    repository_url: str
    visibility: str = "public"


@dataclass(frozen=True)
class RepositorySnapshot:
    """An exact, checked-out local snapshot of a repository at a resolved commit SHA."""
    ref: RepositoryRef
    root_path: Path
    commit_sha: str
    branch: str

    @property
    def root(self) -> Path:
        """Alias for root_path to maintain compatibility with discovery/repo.py Repository model."""
        return self.root_path

    @property
    def name(self) -> str:
        """Repository short name."""
        return self.ref.name

    @property
    def full_name(self) -> str:
        """Repository full name (owner/name)."""
        return self.ref.full_name


@runtime_checkable
class RepositorySource(Protocol):
    """Provider-agnostic interface for discovering, cloning, and updating repositories."""

    def resolve_ref(self, identifier: str, branch: str | None = None) -> RepositoryRef:
        """Parse and normalize a repository identifier."""
        ...

    async def get_metadata(
        self,
        ref: RepositoryRef,
        credential: GitHubCredential | None = None,
    ) -> RepositoryMetadata:
        """Retrieve repository metadata from the remote provider."""
        ...

    async def prepare_snapshot(
        self,
        ref: RepositoryRef,
        target_commit: str | None = None,
        credential: GitHubCredential | None = None,
    ) -> RepositorySnapshot:
        """Clone or prepare a working-copy snapshot checked out at the target commit/branch."""
        ...

    async def update_snapshot(
        self,
        ref: RepositoryRef,
        target_commit: str | None = None,
        credential: GitHubCredential | None = None,
    ) -> RepositorySnapshot:
        """Update an existing working copy via git fetch and checkout without re-cloning."""
        ...

    def compute_diff(
        self,
        snapshot: RepositorySnapshot,
        base_commit: str,
        target_commit: str,
    ) -> list[FileChange]:
        """Compute the set of added, modified, deleted, and renamed files between two commits."""
        ...
