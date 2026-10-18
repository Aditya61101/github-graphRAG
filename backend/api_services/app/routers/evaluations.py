"""Installation-authorized Stage 4.3 API; full validated audit contracts retained."""

# ruff: noqa: B008 -- FastAPI resolves Depends at request time.

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from ai_services.decision_guard.evaluation.models import (
    EvaluationResult,
    EvaluationSettings,
)
from ai_services.decision_guard.orchestration.projections import RepositoryReadModel
from api_services.app.models.pull_requests import PullRequestDetail, PullRequestPage
from api_services.app.utils.github_app_access import authorize_repository_read
from api_services.app.utils.jwt_utils import get_current_user
from api_services.app.utils.processing import require_processing_enabled

router = APIRouter()


async def authorized(
    request: Request, repository_id: str, user=Depends(get_current_user)
):
    await authorize_repository_read(request, user.id, repository_id)
    service = getattr(request.app.state, "evaluation_orchestrator", None)
    if service is None:
        raise HTTPException(503, "PR evaluations are not initialized")
    return service


def owned(service, repository_id, job_id):
    job = service.store.get(job_id)
    if job is None or job.repository_id != repository_id:
        raise HTTPException(404, "Evaluation run not found")
    return job


def summary(job):
    return {
        "deferred": json.loads(job.deferred_json),
        **{
            key: getattr(job, key)
            for key in (
                "id",
                "repository_id",
                "pr_number",
                "revision_key",
                "base_sha",
                "head_sha",
                "status",
                "stage",
                "attempts",
                "max_attempts",
                "lease_until",
                "next_attempt_at",
                "created_at",
                "updated_at",
                "error_code",
                "error_message",
            )
        },
    }


def result_for(job):
    if not job.result_json:
        raise HTTPException(409, "Evaluation result is not available yet")
    return EvaluationResult.model_validate_json(job.result_json)


@router.get("/{repository_id}/evaluations")
async def list_runs(
    repository_id: str,
    pr_number: int | None = Query(None, gt=0),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service=Depends(authorized),
):
    return {
        "runs": [
            summary(job)
            for job in service.store.list(repository_id, pr_number, limit, offset)
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{repository_id}/evaluations/{job_id}")
async def job_status(repository_id: str, job_id: str, service=Depends(authorized)):
    return summary(owned(service, repository_id, job_id))


@router.get(
    "/{repository_id}/evaluations/{job_id}/result", response_model=EvaluationResult
)
async def job_result(repository_id: str, job_id: str, service=Depends(authorized)):
    return result_for(owned(service, repository_id, job_id))


@router.get("/{repository_id}/pull-requests", response_model=PullRequestPage)
async def pull_requests(
    repository_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service=Depends(authorized),
):
    return RepositoryReadModel(service.store).list_prs(repository_id, limit, offset)


@router.get(
    "/{repository_id}/pull-requests/{pr_number}", response_model=PullRequestDetail
)
async def pull_request_detail(
    repository_id: str,
    pr_number: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service=Depends(authorized),
):
    detail = RepositoryReadModel(service.store).detail(
        repository_id, pr_number, limit, offset
    )
    if detail is None:
        raise HTTPException(404, "Pull request not found")
    return detail


class ContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: EvaluationSettings | None = None
    acknowledge_unknown_calls: bool = False
    rebuild_context: bool = False


@router.post("/{repository_id}/evaluations/{job_id}/continue", status_code=202)
async def continue_run(
    repository_id: str,
    job_id: str,
    payload: ContinuationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    service=Depends(authorized),
    user=Depends(get_current_user),
):
    require_processing_enabled()
    # Continuing evaluation can spend tokens and therefore requires the existing
    # monitoring owner/admin guard, not merely collaborator read permission.
    from ai_services.decision_guard.orchestration.dispatch import LocalDispatcher
    from api_services.app.utils.github_app_access import authorize_repository

    await authorize_repository(request, user.id, repository_id, require_admin=True)
    owned(service, repository_id, job_id)
    try:
        service.store.continue_job(
            job_id,
            settings=payload.settings,
            acknowledge_unknown_calls=payload.acknowledge_unknown_calls,
            rebuild_context=payload.rebuild_context,
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    # Recovery also handles a process exit between this commit and dispatch.
    if isinstance(service.dispatcher, LocalDispatcher):
        background_tasks.add_task(service.worker.run_job, job_id)
    return summary(service.store.get(job_id))
