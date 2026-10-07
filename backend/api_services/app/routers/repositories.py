from __future__ import annotations

import logging
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from ai_services.ingestion.adr.service import (
    ADRParsingError,
    ADRService,
    ADRServiceError,
    DuplicateADRError,
    FileSizeExceededError,
    InvalidFileTypeError,
    RepositoryAccessDeniedError as ADRRepoAccessDeniedError,
    RepositoryNotFoundError as ADRRepoNotFoundError,
)
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
import httpx

from ai_services.graph import (
    GraphRepositoryError,
    Neo4jGraphRepository,
    RepositoryAccessDeniedError as GraphRepoAccessDeniedError,
    RepositoryGraphService,
    RepositoryNotFoundError as GraphRepoNotFoundError,
)
from api_services.app.models.adr import ADRResponse
from api_services.app.models.graph import RepositoryGraphResponse
from api_services.app.models.repository import (
    AvailableRepositoryResponse,
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


def get_adr_service(request: Request) -> ADRService:
    service = getattr(request.app.state, "adr_service", None)
    if not service:
        store = get_sqlite_store(request)
        from api_services.app.config import ADRS_STORAGE_DIR, MAX_ADR_FILE_SIZE_BYTES
        service = ADRService(
            sqlite_store=store,
            storage_dir=ADRS_STORAGE_DIR,
            max_file_size_bytes=MAX_ADR_FILE_SIZE_BYTES,
        )
        request.app.state.adr_service = service
    return service


def get_graph_service(request: Request) -> RepositoryGraphService:
    service = getattr(request.app.state, "graph_service", None)
    if not service:
        deps = getattr(request.app.state, "deps", None)
        service = getattr(deps, "graph_service", None) if deps else None
    if not service:
        store = get_sqlite_store(request)
        deps = getattr(request.app.state, "deps", None)
        driver = getattr(deps, "driver", None) if deps else getattr(request.app.state, "driver", None)
        import os
        database = getattr(request.app.state, "database", None) or os.getenv("NEO4J_DATABASE", "neo4j")
        graph_repo = Neo4jGraphRepository(driver=driver, database=database)
        service = RepositoryGraphService(sqlite_store=store, graph_repo=graph_repo)
        request.app.state.graph_service = service
    return service


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


@router.get("/tracked", response_model=list[RepositoryResponse])
async def list_tracked_repositories(
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> list[RepositoryResponse]:
    """List all tracked/indexed repositories belonging to the authenticated user."""
    store = get_sqlite_store(request)
    records = store.list_repositories(user_id=current_user.id)
    return [RepositoryResponse(**record.to_dict()) for record in records]


@router.get("", response_model=list[AvailableRepositoryResponse])
async def list_user_repositories(
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> list[AvailableRepositoryResponse]:
    """List all GitHub repositories of the authenticated user with their DecisionGuard ingestion status."""
    store = get_sqlite_store(request)

    connection = store.get_user_github_connection(current_user.id)
    if not connection or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No connected GitHub account found for the current user. Please authenticate with GitHub first.",
        )

    headers = {
        "Authorization": f"Bearer {connection.access_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DecisionGuard-App",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.github.com/user/repos",
                params={"per_page": 100, "sort": "updated"},
                headers=headers,
            )
    except Exception as exc:
        logger.exception(f"Failed to fetch repositories from GitHub for user '{current_user.id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to reach GitHub API: {exc}",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub access token has expired or is invalid. Please reconnect your GitHub account.",
        )
    elif resp.status_code == 403:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="GitHub API rate limit exceeded or access forbidden.",
        )
    elif resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error: status {resp.status_code}",
        )

    github_repos = resp.json()

    # Load tracked repositories from local SQLite database for this user
    tracked_repos = store.list_repositories(user_id=current_user.id)
    tracked_by_gh_id = {r.github_repository_id: r for r in tracked_repos if r.github_repository_id}
    tracked_by_full_name = {r.full_name.lower(): r for r in tracked_repos if r.full_name}

    results: list[AvailableRepositoryResponse] = []
    seen_tracked_pks: set[str] = set()

    for item in github_repos:
        gh_id = str(item.get("id"))
        full_name = item.get("full_name") or f"{item.get('owner', {}).get('login')}/{item.get('name')}"
        owner = item.get("owner", {}).get("login") or ""
        name = item.get("name") or ""
        is_private = bool(item.get("private", False))

        matched = tracked_by_gh_id.get(gh_id) or tracked_by_full_name.get(full_name.lower())

        if matched:
            seen_tracked_pks.add(matched.id)
            repo_status = matched.status or "COMPLETED"
            tracked_id = matched.id
            commit_sha = matched.indexed_commit_sha
            tracked_branch = matched.tracked_branch
        else:
            repo_status = "NOT_INDEXED"
            tracked_id = None
            commit_sha = None
            tracked_branch = item.get("default_branch") or "main"

        results.append(
            AvailableRepositoryResponse(
                id=tracked_id,
                github_repository_id=gh_id,
                owner=owner,
                name=name,
                full_name=full_name,
                repository_url=item.get("html_url") or f"https://github.com/{full_name}",
                default_branch=item.get("default_branch") or "main",
                tracked_branch=tracked_branch,
                is_private=is_private,
                description=item.get("description"),
                status=repo_status,
                tracked_repository_id=tracked_id,
                indexed_commit_sha=commit_sha,
                updated_at=item.get("updated_at"),
            )
        )

    # Append any tracked repositories that might not have appeared in GitHub page 1
    for tr in tracked_repos:
        if tr.id not in seen_tracked_pks:
            results.append(
                AvailableRepositoryResponse(
                    id=tr.id,
                    github_repository_id=tr.github_repository_id,
                    owner=tr.owner,
                    name=tr.name,
                    full_name=tr.full_name,
                    repository_url=tr.repository_url,
                    default_branch=tr.default_branch,
                    tracked_branch=tr.tracked_branch,
                    is_private=False,
                    description=None,
                    status=tr.status,
                    tracked_repository_id=tr.id,
                    indexed_commit_sha=tr.indexed_commit_sha,
                    updated_at=tr.updated_at.isoformat() if tr.updated_at else None,
                )
            )

    return results


@router.get("/{repo_id}/adrs", response_model=list[ADRResponse])
async def list_repository_adrs(
    repo_id: str,
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> list[ADRResponse]:
    """List all ADRs associated with a repository accessible by the current user."""
    adr_service = get_adr_service(request)
    store = get_sqlite_store(request)
    try:
        repo = adr_service.validate_repository_access(repo_id, current_user.id)
    except ADRRepoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ADRRepoAccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    adrs = store.list_adrs(repository_id=repo.id)
    return [ADRResponse(**adr.to_dict()) for adr in adrs]


@router.get("/{repo_id}/graph", response_model=RepositoryGraphResponse)
async def get_repository_graph(
    repo_id: str,
    request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> RepositoryGraphResponse:
    """Retrieve the architectural entity-relationship graph for a repository."""
    graph_service = get_graph_service(request)
    try:
        return await graph_service.get_repository_graph(
            repository_identifier=repo_id,
            user_id=current_user.id,
        )
    except GraphRepoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except GraphRepoAccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Unexpected error retrieving graph for repository '{repo_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while fetching the repository graph.",
        )


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


@router.post("/{repo_id}/adrs", response_model=ADRResponse, status_code=status.HTTP_201_CREATED)
async def upload_adr(
    repo_id: str,
    request: Request,
    file: UploadFile = File(..., description="ADR document file (.md, .txt, .pdf, .docx)"),
    title: str | None = Form(default=None, description="Optional explicit title for the ADR"),
    description: str | None = Form(default=None, description="Optional brief description"),
    current_user: UserModel = Depends(get_current_user),
) -> Any:
    """Upload and process an Architecture Decision Record (ADR) for a repository."""
    adr_service = get_adr_service(request)
    max_size = adr_service.max_file_size_bytes
    filename = file.filename or "uploaded_adr.txt"

    # Read UploadFile in bounded chunks to prevent unbounded memory allocation
    chunk_size = 64 * 1024  # 64 KB
    chunks: list[bytes] = []
    total_bytes = 0

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_size:
            max_mb = max_size // (1024 * 1024)
            logger.warning(
                f"ADR upload rejected: file '{filename}' exceeded size limit ({total_bytes} > {max_size} bytes)"
            )
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File size exceeds maximum allowed limit of {max_mb} MB.",
            )
        chunks.append(chunk)

    file_bytes = b"".join(chunks)

    try:
        adr_record, _parsed_doc = await adr_service.process_adr_upload(
            repository_id=repo_id,
            user_id=current_user.id,
            filename=filename,
            file_bytes=file_bytes,
            explicit_title=title,
            description=description,
        )
        return ADRResponse(**adr_record.to_dict())
    except ADRRepoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ADRRepoAccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except InvalidFileTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except FileSizeExceededError as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc))
    except DuplicateADRError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ADRParsingError as exc:
        # Internal parser/exception details are logged server-side, not exposed to client
        logger.warning(f"ADR parsing failed for repo '{repo_id}', file '{filename}': {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Failed to parse uploaded ADR document. Please verify the document format and content.",
        )
    except ADRServiceError as exc:
        logger.exception(f"ADR service error for repository '{repo_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while storing or processing the ADR.",
        )
    except Exception as exc:
        logger.exception(f"Unexpected error uploading ADR for repository '{repo_id}': {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the ADR upload.",
        )