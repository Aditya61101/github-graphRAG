from __future__ import annotations

from pydantic import BaseModel, Field


class IngestRepositoryRequest(BaseModel):
    repository: str = Field(
        ...,
        description="GitHub repository identifier, e.g. 'owner/repo' or https://github.com/owner/repo",
    )
    branch: str | None = Field(
        default=None,
        description="Optional branch or ref to index and track. Defaults to the repository's default branch.",
    )
    connection_id: str | None = Field(
        default=None,
        description="Ignored for authorization; credentials are strictly resolved server-side from the authenticated JWT user.",
    )
    force_full: bool = Field(
        default=False,
        description="Force full re-indexing even if this repository was previously indexed.",
    )
    sync: bool = Field(
        default=False,
        description="If True, awaits completion and returns final result. If False (default), runs in background.",
    )


class SyncRepositoryRequest(BaseModel):
    connection_id: str | None = Field(
        default=None,
        description="Ignored for authorization; credentials are strictly resolved server-side from the authenticated JWT user.",
    )
    force_full: bool = Field(
        default=False,
        description="Force full re-indexing rather than an incremental diff sync.",
    )
    sync: bool = Field(
        default=False,
        description="If True, awaits completion and returns final result. If False (default), runs in background.",
    )


class RepositoryResponse(BaseModel):
    id: str
    github_repository_id: str
    owner: str
    name: str
    full_name: str
    default_branch: str
    tracked_branch: str
    repository_url: str
    user_id: str | None = None
    github_connection_id: str | None = None
    indexed_commit_sha: str | None = None
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class AvailableRepositoryResponse(BaseModel):
    github_repository_id: str
    owner: str
    name: str
    full_name: str
    repository_url: str
    default_branch: str
    tracked_branch: str | None = None
    is_private: bool = False
    description: str | None = None
    status: str = Field(
        description="Ingestion status in DecisionGuard: 'NOT_INDEXED', 'INDEXING', 'COMPLETED', 'FAILED', 'IDLE'"
    )
    tracked_repository_id: str | None = Field(
        default=None,
        description="Internal tracked repository ID (e.g. repo_12345) if already in DecisionGuard.",
    )
    indexed_commit_sha: str | None = None
    updated_at: str | None = None


class IngestionJobResponse(BaseModel):
    status: str
    repository: str
    repository_id: str | None = None
    indexed_commit_sha: str | None = None
    is_incremental: bool = False
    added_or_modified_count: int = 0
    deleted_count: int = 0
    message: str = ""
