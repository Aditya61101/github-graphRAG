from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserModel(Base):
    """Represents an authenticated application user."""

    __tablename__ = "users"

    id = Column(String(64), primary_key=True, default=lambda: f"usr_{uuid.uuid4().hex[:12]}")
    username = Column(String(100), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=True, index=True)
    avatar_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    connections = relationship("GitHubConnectionModel", back_populates="user", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "avatar_url": self.avatar_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GitHubConnectionModel(Base):
    """Associates an authenticated user with a GitHub account and OAuth credential."""

    __tablename__ = "github_connections"

    id = Column(String(64), primary_key=True, default=lambda: f"conn_{uuid.uuid4().hex[:12]}")
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    github_user_id = Column(String(64), nullable=False, unique=True, index=True)
    access_token = Column(Text, nullable=False)  # Never expose in API responses or logs!
    token_type = Column(String(32), default="Bearer", nullable=False)
    scope = Column(String(255), nullable=True)
    credential_kind = Column(String(32), default="legacy_oauth", nullable=False)
    access_token_expires_at = Column(DateTime, nullable=True)
    access_status = Column(String(32), default="RECONNECT_REQUIRED", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("UserModel", back_populates="connections")
    repositories = relationship("RepositoryModel", back_populates="github_connection")

    def to_dict(self, include_token: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "github_user_id": self.github_user_id,
            "token_type": self.token_type,
            "scope": self.scope,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_token:
            data["access_token"] = self.access_token
        return data


class RepositoryModel(Base):
    """Represents a connected GitHub repository tracked in DecisionGuard."""

    __tablename__ = "repositories"

    id = Column(String(64), primary_key=True)  # Global immutable identity: repo_{github_repository_id}
    github_repository_id = Column(String(64), nullable=False, unique=True, index=True)
    owner = Column(String(100), nullable=False)
    name = Column(String(100), nullable=False)
    full_name = Column(String(200), nullable=False, unique=True, index=True)
    repository_url = Column(String(500), nullable=False)
    default_branch = Column(String(100), default="main", nullable=False)
    tracked_branch = Column(String(100), default="main", nullable=False)
    user_id = Column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    github_connection_id = Column(
        String(64),
        ForeignKey("github_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    indexed_commit_sha = Column(String(64), nullable=True, index=True)
    installation_id = Column(String(64), ForeignKey("github_installations.id"), nullable=True)
    access_state = Column(String(32), default="RECONNECT_REQUIRED", nullable=False)
    status = Column(String(32), default="IDLE", nullable=False)  # IDLE, INDEXING, COMPLETED, FAILED
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("UserModel", backref="repositories")
    github_connection = relationship("GitHubConnectionModel", back_populates="repositories")
    ingestion_runs = relationship("IngestionRunModel", back_populates="repository", cascade="all, delete-orphan")
    adrs = relationship("ADRModel", back_populates="repository", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_repo_owner_name", "owner", "name"),
        Index("ix_repo_user_fullname", "user_id", "full_name"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "github_repository_id": self.github_repository_id,
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "repository_url": self.repository_url,
            "default_branch": self.default_branch,
            "tracked_branch": self.tracked_branch,
            "user_id": self.user_id,
            "github_connection_id": self.github_connection_id,
            "indexed_commit_sha": self.indexed_commit_sha,
            "installation_id": self.installation_id,
            "access_state": self.access_state,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GitHubInstallationModel(Base):
    __tablename__ = "github_installations"
    id = Column(String(64), primary_key=True)
    app_id = Column(String(64), nullable=False)
    account_id = Column(String(64), nullable=False)
    account_login = Column(String(100), nullable=False)
    account_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    repository_selection = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, nullable=False)
    reconciled_at = Column(DateTime, nullable=True)


class InstallationRepositoryModel(Base):
    __tablename__ = "installation_repositories"
    installation_id = Column(String(64), ForeignKey("github_installations.id"), primary_key=True)
    github_repository_id = Column(String(64), primary_key=True)
    full_name = Column(String(200), nullable=False)
    is_private = Column(Integer, nullable=False)
    granted = Column(Integer, default=1, nullable=False)
    payload = Column(Text, nullable=False)


class ConversationScopeModel(Base):
    __tablename__ = "conversation_scopes"
    user_id = Column(String(64), ForeignKey("users.id"), primary_key=True)
    conversation_id = Column(String(255), primary_key=True)
    repository_id = Column(String(64), ForeignKey("repositories.id"), nullable=False)


class PullRequestRevisionModel(Base):
    """Operational metadata only; never source contents or serialized change sets."""
    __tablename__ = 'pull_request_revisions'
    revision_key = Column(String(64), primary_key=True)
    repository_id = Column(String(64), ForeignKey('repositories.id', ondelete='CASCADE'), nullable=False)
    github_repository_id = Column(String(64), nullable=False)
    pull_request_number = Column(Integer, nullable=False)
    base_sha = Column(String(40), nullable=False)
    head_sha = Column(String(40), nullable=False)
    delivery_id = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False)
    attempts = Column(Integer, nullable=False, default=1)
    started_at = Column(DateTime, nullable=False, default=utc_now)
    completed_at = Column(DateTime, nullable=True)
    error_type = Column(String(100), nullable=True)
    error = Column(String(500), nullable=True)


class IngestionRunModel(Base):
    """Tracks operational state and history for repository ingestion jobs."""

    __tablename__ = "ingestion_runs"

    id = Column(String(64), primary_key=True, default=lambda: f"run_{uuid.uuid4().hex[:12]}")
    repository_id = Column(String(64), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    base_commit_sha = Column(String(64), nullable=True)
    target_commit_sha = Column(String(64), nullable=False)
    status = Column(String(32), default="INDEXING", nullable=False)  # INDEXING, COMPLETED, FAILED
    trigger = Column(String(32), default="manual", nullable=False)  # initial, webhook_push, manual
    is_incremental = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, default=utc_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    repository = relationship("RepositoryModel", back_populates="ingestion_runs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "base_commit_sha": self.base_commit_sha,
            "target_commit_sha": self.target_commit_sha,
            "status": self.status,
            "trigger": self.trigger,
            "is_incremental": bool(self.is_incremental),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }


class ADRModel(Base):
    """Represents an Architecture Decision Record (ADR) associated with a repository."""

    __tablename__ = "adrs"

    id = Column(String(64), primary_key=True, default=lambda: f"adr_{uuid.uuid4().hex[:12]}")
    repository_id = Column(
        String(64),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(32), default="PENDING", nullable=False)  # PENDING, PROCESSING, COMPLETED, FAILED

    # Source metadata (designed for MANUAL_UPLOAD, CONFLUENCE, etc.)
    source_type = Column(String(32), default="MANUAL_UPLOAD", nullable=False)
    source_id = Column(String(255), nullable=True)
    source_url = Column(String(500), nullable=True)
    source_name = Column(String(255), nullable=False)  # Original uploaded filename or page title
    source_version = Column(String(64), nullable=True)

    # Local file metadata
    file_path = Column(String(500), nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    file_size = Column(Integer, nullable=True)
    mime_type = Column(String(100), nullable=True)
    file_extension = Column(String(32), nullable=True)

    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    repository = relationship("RepositoryModel", back_populates="adrs")

    __table_args__ = (
        Index("ix_adr_repo_id", "repository_id"),
        Index("ix_adr_repo_hash", "repository_id", "content_hash"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_name": self.source_name,
            "source_version": self.source_version,
            "file_path": self.file_path,
            "content_hash": self.content_hash,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "file_extension": self.file_extension,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
