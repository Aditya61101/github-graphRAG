from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from ai_services.ingestion.persistence.database import get_session_factory, init_db
from ai_services.ingestion.persistence.models import (
    GitHubConnectionModel,
    IngestionRunModel,
    RepositoryModel,
    UserModel,
    utc_now,
)
from ai_services.ingestion.sources.credentials import CredentialProvider, GitHubCredential
from ai_services.ingestion.sources.interface import RepositoryMetadata

logger = logging.getLogger(__name__)


class SqliteApplicationStore:
    """Relational application persistence store backed by SQLite (SQLAlchemy ORM).

    Source of truth for:
    - User identities
    - GitHub connections & user OAuth credentials
    - Tracked repository configurations
    - Ingestion execution runs and indexed commit SHAs
    """

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        init_db()
        self.session_factory = session_factory or get_session_factory()

    def upsert_github_login(
        self,
        github_user_id: str,
        username: str,
        email: str | None = None,
        avatar_url: str | None = None,
        access_token: str = "",
        token_type: str = "Bearer",
        scope: str | None = None,
    ) -> tuple[UserModel, GitHubConnectionModel]:
        """Upsert application user and GitHub connection from GitHub OAuth callback.

        Enforces:
        - If GitHubConnection exists for github_user_id: update existing User and connection.
        - Repeated logins by the same GitHub account do NOT create duplicate User rows.
        """
        import uuid

        with self.session_factory() as session:
            stmt = select(GitHubConnectionModel).where(
                GitHubConnectionModel.github_user_id == str(github_user_id)
            )
            conn = session.scalars(stmt).first()

            if conn:
                user = session.get(UserModel, conn.user_id)
                if not user:
                    user = UserModel(
                        id=conn.user_id,
                        username=username,
                        email=email,
                        avatar_url=avatar_url,
                    )
                    session.add(user)
                else:
                    user.username = username
                    user.email = email or user.email
                    user.avatar_url = avatar_url or user.avatar_url
                    user.updated_at = utc_now()

                conn.access_token = access_token
                conn.token_type = token_type
                conn.scope = scope
                conn.updated_at = utc_now()
            else:
                user_stmt = select(UserModel).where(UserModel.username == username)
                user = session.scalars(user_stmt).first()
                if not user:
                    user = UserModel(
                        id=f"usr_{uuid.uuid4().hex[:12]}",
                        username=username,
                        email=email,
                        avatar_url=avatar_url,
                    )
                    session.add(user)
                    session.flush()
                else:
                    user.email = email or user.email
                    user.avatar_url = avatar_url or user.avatar_url
                    user.updated_at = utc_now()

                conn = GitHubConnectionModel(
                    user_id=user.id,
                    github_user_id=str(github_user_id),
                    access_token=access_token,
                    token_type=token_type,
                    scope=scope,
                )
                session.add(conn)

            session.commit()
            session.refresh(user)
            session.refresh(conn)
            return user, conn

    def get_user(self, user_id: str) -> UserModel | None:
        """Fetch an application user by primary key."""
        with self.session_factory() as session:
            return session.get(UserModel, user_id)

    def get_user_github_connection(self, user_id: str) -> GitHubConnectionModel | None:
        """Fetch the active GitHub connection for an authenticated application user."""
        with self.session_factory() as session:
            stmt = (
                select(GitHubConnectionModel)
                .where(GitHubConnectionModel.user_id == user_id)
                .order_by(GitHubConnectionModel.updated_at.desc())
            )
            return session.scalars(stmt).first()

    def get_credential_for_user(self, user_id: str) -> GitHubCredential | None:
        """Resolve a GitHubCredential for the specified application user."""
        conn = self.get_user_github_connection(user_id)
        if conn and conn.access_token:
            return GitHubCredential(token=conn.access_token, token_type=conn.token_type)
        return None

    def upsert_user(
        self,
        username: str,
        email: str | None = None,
        avatar_url: str | None = None,
        user_id: str | None = None,
    ) -> UserModel:
        """Create or update an application user."""
        with self.session_factory() as session:
            stmt = select(UserModel).where(
                or_(UserModel.username == username, UserModel.id == user_id)
            ) if user_id else select(UserModel).where(UserModel.username == username)
            user = session.scalars(stmt).first()

            if user:
                user.email = email or user.email
                user.avatar_url = avatar_url or user.avatar_url
                user.updated_at = utc_now()
            else:
                user = UserModel(
                    id=user_id or f"usr_{username}",
                    username=username,
                    email=email,
                    avatar_url=avatar_url,
                )
                session.add(user)

            session.commit()
            session.refresh(user)
            return user

    def upsert_github_connection(
        self,
        user_id: str,
        github_user_id: str,
        access_token: str,
        token_type: str = "Bearer",
        scope: str | None = None,
    ) -> GitHubConnectionModel:
        """Associate a user's GitHub identity and OAuth token with their user account."""
        with self.session_factory() as session:
            stmt = select(GitHubConnectionModel).where(
                GitHubConnectionModel.user_id == user_id,
                GitHubConnectionModel.github_user_id == str(github_user_id),
            )
            conn = session.scalars(stmt).first()

            if conn:
                conn.access_token = access_token
                conn.token_type = token_type
                conn.scope = scope
                conn.updated_at = utc_now()
            else:
                conn = GitHubConnectionModel(
                    user_id=user_id,
                    github_user_id=str(github_user_id),
                    access_token=access_token,
                    token_type=token_type,
                    scope=scope,
                )
                session.add(conn)

            session.commit()
            session.refresh(conn)
            return conn

    def save_repository(
        self,
        metadata: RepositoryMetadata,
        user_id: str | None = None,
        connection_id: str | None = None,
    ) -> RepositoryModel:
        """Register or update a repository record using its immutable GitHub ID."""
        github_repo_id = str(metadata.github_repository_id)
        repo_pk = f"repo_{github_repo_id}"

        with self.session_factory() as session:
            stmt = select(RepositoryModel).where(
                or_(
                    RepositoryModel.id == repo_pk,
                    RepositoryModel.github_repository_id == github_repo_id,
                    RepositoryModel.full_name == metadata.full_name,
                )
            )
            repo = session.scalars(stmt).first()

            if repo:
                # Requirement 2: Do not silently transfer repository ownership
                if repo.user_id and user_id and repo.user_id != user_id:
                    from ai_services.ingestion.sources.interface import RepositoryOwnershipConflictError
                    raise RepositoryOwnershipConflictError(
                        f"Repository '{metadata.full_name}' is already connected and owned by another user. "
                        "Ownership transfer is prohibited."
                    )

                repo.github_repository_id = github_repo_id
                repo.owner = metadata.owner
                repo.name = metadata.name
                repo.full_name = metadata.full_name
                repo.repository_url = metadata.repository_url
                repo.default_branch = metadata.default_branch
                if metadata.tracked_branch:
                    repo.tracked_branch = metadata.tracked_branch

                # Do NOT overwrite repo.user_id or repo.github_connection_id with another user
                if not repo.user_id and user_id:
                    repo.user_id = user_id
                if not repo.github_connection_id and connection_id:
                    repo.github_connection_id = connection_id
                elif repo.user_id == user_id and connection_id:
                    # Same owner refreshing their active connection
                    repo.github_connection_id = connection_id

                repo.updated_at = utc_now()
            else:
                repo = RepositoryModel(
                    id=repo_pk,
                    github_repository_id=github_repo_id,
                    owner=metadata.owner,
                    name=metadata.name,
                    full_name=metadata.full_name,
                    repository_url=metadata.repository_url,
                    default_branch=metadata.default_branch,
                    tracked_branch=metadata.tracked_branch or metadata.default_branch,
                    user_id=user_id,
                    github_connection_id=connection_id,
                    status="IDLE",
                )
                session.add(repo)

            session.commit()
            session.refresh(repo)
            return repo

    def get_repository(
        self,
        identifier_or_user_id: str,
        repository_identifier: str | None = None,
        user_id: str | None = None,
    ) -> RepositoryModel | None:
        """Fetch repository by ID (repo_12345), github_repository_id, or full_name (owner/name).

        Supports:
        - get_repository(user_id, repository_identifier)
        - get_repository(repository_identifier, user_id=user_id)
        - get_repository(repository_identifier) (unscoped, e.g. for webhooks)
        """
        if repository_identifier is not None:
            uid = identifier_or_user_id
            ident = repository_identifier
        else:
            uid = user_id
            ident = identifier_or_user_id

        with self.session_factory() as session:
            stmt = select(RepositoryModel).where(
                or_(
                    RepositoryModel.id == ident,
                    RepositoryModel.github_repository_id == ident,
                    RepositoryModel.full_name == ident,
                )
            )
            if uid:
                stmt = stmt.where(
                    or_(
                        RepositoryModel.user_id == uid,
                        RepositoryModel.github_connection.has(user_id=uid),
                    )
                )
            return session.scalars(stmt).first()

    def list_repositories(self, user_id: str | None = None) -> list[RepositoryModel]:
        """Return repositories belonging to the specified user, or all repositories if unconstrained."""
        with self.session_factory() as session:
            stmt = select(RepositoryModel)
            if user_id:
                stmt = stmt.where(
                    or_(
                        RepositoryModel.user_id == user_id,
                        RepositoryModel.github_connection.has(user_id=user_id),
                    )
                )
            stmt = stmt.order_by(RepositoryModel.full_name)
            return list(session.scalars(stmt).all())

    def record_ingestion_start(
        self,
        repository_id: str,
        target_commit_sha: str,
        base_commit_sha: str | None = None,
        trigger: str = "manual",
        is_incremental: bool = False,
    ) -> IngestionRunModel:
        """Record the start of an ingestion run and transition repository to INDEXING."""
        with self.session_factory() as session:
            repo = session.get(RepositoryModel, repository_id)
            if not repo:
                # Try finding by full_name or github_id
                repo = self.get_repository(repository_id)
                if repo:
                    repo = session.get(RepositoryModel, repo.id)

            if repo:
                repo.status = "INDEXING"
                repo.updated_at = utc_now()

            run = IngestionRunModel(
                repository_id=repo.id if repo else repository_id,
                base_commit_sha=base_commit_sha,
                target_commit_sha=target_commit_sha,
                status="INDEXING",
                trigger=trigger,
                is_incremental=1 if is_incremental else 0,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def record_ingestion_success(
        self,
        repository_id: str,
        run_id: str,
        commit_sha: str,
    ) -> None:
        """Mark ingestion run and repository as COMPLETED and update indexed_commit_sha."""
        with self.session_factory() as session:
            run = session.get(IngestionRunModel, run_id)
            if run:
                run.status = "COMPLETED"
                run.completed_at = utc_now()
                run.error = None

            repo = session.get(RepositoryModel, repository_id)
            if not repo:
                repo = self.get_repository(repository_id)
                if repo:
                    repo = session.get(RepositoryModel, repo.id)

            if repo:
                repo.status = "COMPLETED"
                repo.indexed_commit_sha = commit_sha
                repo.updated_at = utc_now()

            session.commit()

    def record_ingestion_failure(
        self,
        repository_id: str,
        run_id: str,
        error: str,
    ) -> None:
        """Mark ingestion run and repository as FAILED without advancing indexed_commit_sha."""
        with self.session_factory() as session:
            run = session.get(IngestionRunModel, run_id)
            if run:
                run.status = "FAILED"
                run.completed_at = utc_now()
                run.error = str(error)

            repo = session.get(RepositoryModel, repository_id)
            if not repo:
                repo = self.get_repository(repository_id)
                if repo:
                    repo = session.get(RepositoryModel, repo.id)

            if repo:
                repo.status = "FAILED"
                repo.updated_at = utc_now()

            session.commit()

    def get_credential_by_repository_id(self, repository_id: str) -> GitHubCredential | None:
        """Resolve the GitHub OAuth credential strictly associated with a tracked repository ID.

        Never select a GitHub credential merely from a repository name.
        """
        with self.session_factory() as session:
            repo = session.get(RepositoryModel, repository_id)
            if not repo:
                stmt = select(RepositoryModel).where(RepositoryModel.github_repository_id == repository_id)
                repo = session.scalars(stmt).first()
            if not repo:
                return None

            conn = None
            if repo.github_connection_id:
                conn = session.get(GitHubConnectionModel, repo.github_connection_id)
            elif repo.user_id:
                stmt = (
                    select(GitHubConnectionModel)
                    .where(GitHubConnectionModel.user_id == repo.user_id)
                    .order_by(GitHubConnectionModel.updated_at.desc())
                )
                conn = session.scalars(stmt).first()

            if conn and conn.access_token:
                return GitHubCredential(
                    token=conn.access_token,
                    token_type=conn.token_type,
                )
            return None

    def get_credential_for_repository(self, repository_id: str) -> GitHubCredential | None:
        """Alias delegating to get_credential_by_repository_id (never resolves by repository name)."""
        return self.get_credential_by_repository_id(repository_id)

    def get_credential_by_connection_id(self, connection_id: str) -> GitHubCredential | None:
        """Resolve a credential strictly for the given connection ID."""
        with self.session_factory() as session:
            conn = session.get(GitHubConnectionModel, connection_id)
            if conn and conn.access_token:
                return GitHubCredential(token=conn.access_token, token_type=conn.token_type)
            return None


class SqliteCredentialProvider(CredentialProvider):
    """Adapter implementing CredentialProvider over SqliteApplicationStore."""

    def __init__(self, store: SqliteApplicationStore) -> None:
        self.store = store

    def get_credential(
        self,
        connection_id: str | None = None,
        user_id: str | None = None,
    ) -> GitHubCredential | None:
        """Strictly resolve credential for specified connection_id or user_id.

        Never falls back to unrelated user connections or global tokens.
        """
        with self.store.session_factory() as session:
            if connection_id:
                conn = session.get(GitHubConnectionModel, connection_id)
                if conn and conn.access_token:
                    return GitHubCredential(token=conn.access_token, token_type=conn.token_type)

            if user_id:
                stmt = (
                    select(GitHubConnectionModel)
                    .where(GitHubConnectionModel.user_id == user_id)
                    .order_by(GitHubConnectionModel.updated_at.desc())
                )
                conn = session.scalars(stmt).first()
                if conn and conn.access_token:
                    return GitHubCredential(token=conn.access_token, token_type=conn.token_type)

            return None
