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
    status = Column(String(32), default="IDLE", nullable=False)  # IDLE, INDEXING, COMPLETED, FAILED
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("UserModel", backref="repositories")
    github_connection = relationship("GitHubConnectionModel", back_populates="repositories")
    ingestion_runs = relationship("IngestionRunModel", back_populates="repository", cascade="all, delete-orphan")

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
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
