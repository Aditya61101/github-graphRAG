"""Private Cloud Run service. IAM must require authenticated invocation.

uvicorn ai_services.runs.evaluation_service:app --host 0.0.0.0 --port 8080
No public routes are mounted here. Cloud Run verifies the OIDC invoker identity.
"""

import asyncio
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ai_services.decision_guard.orchestration.checks import CheckPublisher
from ai_services.decision_guard.orchestration.dispatch import EvaluationOrchestrator
from ai_services.decision_guard.orchestration.gcp import configured_dispatcher, validate_remote_configuration
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from shared.utils.processing import background_processing_enabled

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Also bound overlapping invocations within a process (including reconciliation).
slot = asyncio.Semaphore(1)


class EvaluationRequest(BaseModel):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def require_enabled():
    if not background_processing_enabled():
        raise HTTPException(503, "Processing disabled")
    validate_remote_configuration()


@app.post("/evaluate")
async def evaluate(request: EvaluationRequest):
    require_enabled()
    from ai_services.runs.run_pr_evaluation import run

    async with slot:
        jobs = EvaluationStore(SqliteApplicationStore().session_factory)
        if jobs.get(request.job_id) is None:
            raise HTTPException(404, "Unknown evaluation")
        try:
            # Cancel before Cloud Tasks' 1800s deadline; the durable lease and
            # checkpoints survive timeout/restart and provider ambiguity is retained.
            async with asyncio.timeout(1680):
                await run(request.job_id)
        except TimeoutError:
            raise HTTPException(503, "Evaluation timed out; recovery pending") from None
        job = jobs.get(request.job_id)
        if job.status in {"QUEUED", "RETRY", "RUNNING"}:
            raise HTTPException(503, "Evaluation recovery pending")
        return {"status": job.status}


@app.post("/reconcile")
async def reconcile():
    require_enabled()
    async with slot, httpx.AsyncClient(timeout=30) as http:
        store = SqliteApplicationStore()
        jobs = EvaluationStore(store.session_factory)
        github = GitHubAppService(store, GitHubAppSettings.from_env(), http)
        dispatcher = configured_dispatcher(http)
        if dispatcher is None:
            raise HTTPException(503, "Remote dispatcher required")
        publisher = CheckPublisher(jobs, github,
            enabled=os.getenv("GITHUB_CHECKS_ENABLED", "false").lower() == "true",
            details_base=os.getenv("GITHUB_CHECKS_DETAILS_BASE") or None)
        await EvaluationOrchestrator(jobs, None, dispatcher=dispatcher,
            publisher=publisher, lifecycle_github=github).reconcile()
    return {"status": "reconciled"}
