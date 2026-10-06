from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ai_services.ingestion.persistence.models import UserModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.service import IngestionResult, RepositoryIngestionService
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.ingestion.sources.interface import (
    PrivateRepositoryUnsupportedError,
    RepositoryAuthenticationError,
    RepositoryNotFoundError,
    RepositoryOwnershipConflictError,
)
from api_services.app.models.repository import (
    IngestRepositoryRequest,
    IngestionJobResponse,
    RepositoryResponse,
    SyncRepositoryRequest,
)
from api_services.app.utils.jwt_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def get_ingestion_service(request: Request) -> RepositoryIngestionService:
    service = getattr(request.app.state, "ingestion_service", None)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository ingestion service is not initialized.",
        )
    return service


def get_sqlite_store(request: Request) -> SqliteApplicationStore:
    store = getattr(request.app.state, "sqlite_store", None)
    if not store:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SQLite application store is not initialized.",
        )
    return store


async def _run_ingest_background(
    service: RepositoryIngestionService,
    repo_identifier: str,
    branch: str | None,
    force_full: bool,
    credential: GitHubCredential | None,
    user_id: str | None,
    connection_id: str | None,
    trigger: str = "manual",
) -> None:
    try:
        result = await service.ingest_repository(
            repo_identifier=repo_identifier,
            branch=branch,
            force_full=force_full,
            credential=credential,
            user_id=user_id,
            connection_id=connection_id,
            trigger=trigger,
        )
        logger.info(
            f"Background ingestion finished for '{repo_identifier}': status={result.status}, msg={result.message}"
        )
    except Exception as exc:
        logger.exception(f"Background ingestion failed for '{repo_identifier}': {exc}")


@router.post("/ingest", response_model=IngestionJobResponse)
async def ingest_repository(
    payload: IngestRepositoryRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: UserModel = Depends(get_current_user),
) -> Any:
    """Trigger initial or re-indexing of a GitHub repository with authenticated user credentials."""
    service = get_ingestion_service(request)
    store = get_sqlite_store(request)

    # Resolve credential server-side from authenticated user (never accept client tokens or connection IDs)
    connection = store.get_user_github_connection(current_user.id)
    print("Current connection:", connection.user_id if connection else None)
    print("connection token type:", connection.token_type if connection else None)
    print("connection access token:", connection.access_token if connection else None)
    
    if not connection or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No connected GitHub account found for the current user. Please authenticate with GitHub first.",
        )
    credential = GitHubCredential(token=connection.access_token, token_type=connection.token_type)

    if not payload.sync:
        background_tasks.add_task(
            _run_ingest_background,
            service,
            payload.repository,
            payload.branch,
            payload.force_full,
            credential,
            current_user.id,
            connection.id,
            "manual",
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=IngestionJobResponse(
                status="accepted",
                repository=payload.repository,
                message="Repository ingestion has been queued in the background.",
            ).model_dump(),
        )

    try:
        result: IngestionResult = await service.ingest_repository(
            repo_identifier=payload.repository,
            branch=payload.branch,
            force_full=payload.force_full,
            credential=credential,
            user_id=current_user.id,
            connection_id=connection.id,
            trigger="manual",
        )
        return IngestionJobResponse(
            status=result.status,
            repository=result.repository,
            repository_id=result.repository_id,
            indexed_commit_sha=result.indexed_commit_sha,
            is_incremental=result.is_incremental,
            added_or_modified_count=result.added_or_modified_count,
            deleted_count=result.deleted_count,
            message=result.message,
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except RepositoryAuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except PrivateRepositoryUnsupportedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RepositoryOwnershipConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Ingestion failed for {payload.repository}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {exc}",
        )


@router.get("", response_model=list[RepositoryResponse])
async def list_repositories(
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> list[RepositoryResponse]:
    """List all tracked repositories belonging to the authenticated user."""
    store = get_sqlite_store(request)
    records = store.list_repositories(user_id=current_user.id)
    return [RepositoryResponse(**record.to_dict()) for record in records]


@router.get("/{owner}/{name}", response_model=RepositoryResponse)
async def get_repository(
    owner: str,
    name: str,
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> RepositoryResponse:
    """Retrieve details and indexing status of a specific repository owned by the authenticated user."""
    store = get_sqlite_store(request)
    full_name = f"{owner}/{name}"
    record = store.get_repository(full_name, user_id=current_user.id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{full_name}' not found for current user.",
        )
    return RepositoryResponse(**record.to_dict())


@router.post("/{owner}/{name}/sync", response_model=IngestionJobResponse)
async def sync_repository(
    owner: str,
    name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SyncRepositoryRequest | None = None,
    current_user: UserModel = Depends(get_current_user),
) -> Any:
    """Trigger manual synchronization (incremental diff or full re-indexing) for a repository owned by current user."""
    service = get_ingestion_service(request)
    store = get_sqlite_store(request)

    full_name = f"{owner}/{name}"
    record = store.get_repository(full_name, user_id=current_user.id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{full_name}' not found or access denied. Please ingest it first.",
        )

    # Resolve credential server-side from authenticated user's GitHub connection
    connection = store.get_user_github_connection(current_user.id)
    if not connection or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active GitHub connection found for current user. Please reconnect GitHub.",
        )

    credential = GitHubCredential(token=connection.access_token, token_type=connection.token_type)

    force_full = payload.force_full if payload else False
    is_sync = payload.sync if payload else False

    if not is_sync:
        background_tasks.add_task(
            _run_ingest_background,
            service,
            full_name,
            record.tracked_branch,
            force_full,
            credential,
            current_user.id,
            connection.id,
            "manual_sync",
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=IngestionJobResponse(
                status="accepted",
                repository=full_name,
                repository_id=record.id,
                message=f"Sync job for '{full_name}' has been queued in the background.",
            ).model_dump(),
        )

    try:
        result: IngestionResult = await service.ingest_repository(
            repo_identifier=full_name,
            branch=record.tracked_branch,
            force_full=force_full,
            credential=credential,
            user_id=current_user.id,
            connection_id=connection.id,
            trigger="manual_sync",
        )
        return IngestionJobResponse(
            status=result.status,
            repository=result.repository,
            repository_id=result.repository_id,
            indexed_commit_sha=result.indexed_commit_sha,
            is_incremental=result.is_incremental,
            added_or_modified_count=result.added_or_modified_count,
            deleted_count=result.deleted_count,
            message=result.message,
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except RepositoryAuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except PrivateRepositoryUnsupportedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RepositoryOwnershipConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Manual sync failed for {full_name}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {exc}",
        )
