from __future__ import annotations

import logging
from typing import Any
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.models import UserModel
from ai_services.ingestion.sources.interface import RepositoryOwnershipConflictError, RepositorySourceError
from api_services.app.models.adr import ADRResponse, ADRBatchUploadResponse, ADRUploadItemResponse
from api_services.app.models.graph import RepositoryGraphResponse
from api_services.app.models.query import APIResponse, QueryRequest
from api_services.app.models.repository import (
    AvailableRepositoryResponse, IngestRepositoryRequest, IngestionJobResponse,
    RepositoryResponse, SyncRepositoryRequest,
)
from api_services.app.utils.github_app_access import get_github_app, authorize_repository, query_authorized
from api_services.app.utils.jwt_utils import get_current_user
from api_services.app.utils.query_response_mapper import build_api_response

logger = logging.getLogger(__name__)
router = APIRouter()


def resource(request, name):
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(503, f'{name} is not initialized')
    return service


async def _index(service, repo_id, installation_id, user_id, branch, force_full, trigger):
    return await service.ingest_repository(
        repo_identifier=repo_id, repository_id=repo_id, installation_id=installation_id,
        user_id=user_id, branch=branch, force_full=force_full, trigger=trigger,
    )


async def _run_ingest_background(service, repo_id, installation_id, user_id, branch, force_full, trigger):
    try:
        await _index(service, repo_id, installation_id, user_id, branch, force_full, trigger)
    except Exception:
        logger.exception('Background indexing failed repository=%s', repo_id)


async def _start_index(request, background_tasks, user, repo, branch, force_full, sync, trigger):
    service = resource(request, 'ingestion_service')
    args = (service, repo.id, repo.installation_id, user.id, branch, force_full, trigger)
    if not sync:
        background_tasks.add_task(_run_ingest_background, *args)
        return JSONResponse(status_code=202, content=IngestionJobResponse(
            status='accepted', repository=repo.full_name, repository_id=repo.id,
            message='Indexing scheduled with execution-time installation authorization.',
        ).model_dump())
    try:
        result = await _index(*args)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except RepositorySourceError:
        raise HTTPException(400, 'Repository could not be indexed; verify public access and branch') from None
    except Exception:
        logger.exception('Indexing failed repository=%s', repo.id)
        raise HTTPException(500, 'Repository indexing failed') from None
    return IngestionJobResponse(**vars(result))


@router.post('/ingest', response_model=IngestionJobResponse)
async def ingest_repository(payload: IngestRepositoryRequest, request: Request,
                            background_tasks: BackgroundTasks,
                            current_user: UserModel = Depends(get_current_user)) -> Any:
    app_service = get_github_app(request)
    try:
        selected = await app_service.select_repository(current_user.id, payload.repository, payload.installation_id)
        repo = app_service.bind_repository(selected, current_user.id, payload.branch)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except ValueError:
        raise HTTPException(400, 'Invalid repository identifier') from None
    except RepositoryOwnershipConflictError:
        raise HTTPException(409, 'Repository is owned by another DecisionGuard user') from None
    return await _start_index(request, background_tasks, current_user, repo, repo.tracked_branch,
                              payload.force_full, payload.sync, 'manual')


@router.get('/tracked', response_model=list[RepositoryResponse])
async def list_tracked_repositories(request: Request, current_user: UserModel = Depends(get_current_user)):
    try:
        _, accessible = await get_github_app(request).discovery(current_user.id)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    grants = {(str(item['id']), item['installation_id']) for item in accessible}
    records = resource(request, 'sqlite_store').list_repositories(user_id=current_user.id)
    return [RepositoryResponse(**record.to_dict()) for record in records
            if record.user_id == current_user.id and (record.github_repository_id, record.installation_id) in grants]


@router.get('', response_model=list[AvailableRepositoryResponse])
async def list_user_repositories(request: Request, current_user: UserModel = Depends(get_current_user)):
    try:
        _, accessible = await get_github_app(request).discovery(current_user.id)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    tracked = {record.github_repository_id: record for record in
               resource(request, 'sqlite_store').list_repositories(user_id=current_user.id)
               if record.user_id == current_user.id}
    output, seen = [], set()
    for item in accessible:
        rid = str(item['id'])
        if rid in seen:
            continue
        seen.add(rid)
        local = tracked.get(rid)
        output.append(AvailableRepositoryResponse(
            id=local.id if local else None, github_repository_id=rid,
            installation_id=item['installation_id'], owner=item['owner']['login'], name=item['name'],
            full_name=item['full_name'], repository_url=item['html_url'],
            default_branch=item.get('default_branch') or 'main',
            tracked_branch=local.tracked_branch if local else item.get('default_branch') or 'main',
            is_private=False, description=item.get('description'),
            status=local.status if local else 'NOT_INDEXED', tracked_repository_id=local.id if local else None,
            indexed_commit_sha=local.indexed_commit_sha if local else None, updated_at=item.get('updated_at'),
        ))
    return output


@router.get('/{repo_id}/adrs', response_model=list[ADRResponse])
async def list_repository_adrs(repo_id: str, request: Request,
                               current_user: UserModel = Depends(get_current_user)):
    repo = await authorize_repository(request, current_user.id, repo_id)
    return [ADRResponse(**adr.to_dict()) for adr in resource(request, 'sqlite_store').list_adrs(repo.id)]


@router.get('/{repo_id}/graph', response_model=RepositoryGraphResponse)
async def get_repository_graph(repo_id: str, request: Request,
                               current_user: UserModel = Depends(get_current_user)):
    repo = await authorize_repository(request, current_user.id, repo_id)
    try:
        return await resource(request, 'graph_service').get_repository_graph(repo.id, current_user.id)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Graph fetch failed repository=%s', repo.id)
        raise HTTPException(500, 'An error occurred while fetching the repository graph') from None


@router.post('/{repo_id}/query', response_model=APIResponse)
async def query_repository(repo_id: str, payload: QueryRequest, http_request: Request,
                           current_user: UserModel = Depends(get_current_user)):
    if payload.repository_id and payload.repository_id != repo_id:
        raise HTTPException(400, 'Query repository does not match URL scope')
    result = await query_authorized(http_request, current_user, repo_id, payload)
    return build_api_response(result=result, conversation_id=payload.conversation_id)


@router.get('/{owner}/{name}', response_model=RepositoryResponse)
async def get_repository(owner: str, name: str, request: Request,
                         current_user: UserModel = Depends(get_current_user)):
    repo = await authorize_repository(request, current_user.id, f'{owner}/{name}')
    return RepositoryResponse(**repo.to_dict())


@router.post('/{owner}/{name}/sync', response_model=IngestionJobResponse)
async def sync_repository(owner: str, name: str, request: Request, background_tasks: BackgroundTasks,
                          payload: SyncRepositoryRequest | None = None,
                          current_user: UserModel = Depends(get_current_user)):
    repo = await authorize_repository(request, current_user.id, f'{owner}/{name}')
    payload = payload or SyncRepositoryRequest()
    return await _start_index(request, background_tasks, current_user, repo, repo.tracked_branch,
                              payload.force_full, payload.sync, 'manual_sync')


@router.post('/{repo_id}/adrs', response_model=ADRBatchUploadResponse, status_code=201,
             responses={207: {'model': ADRBatchUploadResponse, 'description': 'Per-file failures or duplicates'}})
async def upload_adr(repo_id: str, request: Request,
                     files: list[UploadFile] = File(..., description='One or more ADR documents (.md, .txt, .pdf, .docx)'),
                     current_user: UserModel = Depends(get_current_user)) -> Any:
    repo = await authorize_repository(request, current_user.id, repo_id)
    try:
        items = await resource(request, 'adr_service').process_adr_uploads(repo.id, current_user.id, files)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    result = ADRBatchUploadResponse(
        repository_id=repo.id, total=len(items),
        completed=sum(item.status == 'completed' for item in items),
        duplicates=sum(item.status == 'duplicate' for item in items),
        failed=sum(item.status == 'failed' for item in items),
        results=[ADRUploadItemResponse(
            index=item.index, filename=item.filename, status=item.status, status_code=item.status_code,
            error=item.error, adr=ADRResponse(**item.adr.to_dict()) if item.adr else None,
        ) for item in items],
    )
    return JSONResponse(status_code=201 if result.completed == result.total else 207,
                        content=result.model_dump(mode='json'))
