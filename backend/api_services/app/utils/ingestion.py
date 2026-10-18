"""HTTP acceptance commits existing ingestion history before remote dispatch."""
import logging
import httpx
from fastapi import HTTPException
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
from ai_services.ingestion.dispatch import configured_dispatcher


async def dispatch_one(run_id):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await configured_dispatcher(client).dispatch(run_id)
    except Exception as exc:
        logging.getLogger(__name__).warning("Ingestion dispatch deferred type=%s", type(exc).__name__)


async def accept(request, tasks, repo, *, target=None, force_full=False,
                 trigger="manual", delivery_id=None):
    from api_services.app.utils.processing import require_processing_enabled
    require_processing_enabled()
    store = getattr(request.app.state, "sqlite_store", None)
    service = getattr(request.app.state, "ingestion_service", None)
    if store is None or service is None:
        raise HTTPException(503, "Repository ingestion service is not initialized")
    if target is None:
        from ai_services.github_app.service import GitHubAppError
        from ai_services.ingestion.sources.interface import RepositorySourceError
        try:
            source = service.repository_source
            credential = await request.app.state.github_app.indexing_credential(repo.id, repo.installation_id)
            target = await source.resolve_target(source.resolve_ref(repo.full_name, branch=repo.tracked_branch),
                                                 credential)
        except GitHubAppError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        except RepositorySourceError:
            raise HTTPException(503, "Tracked branch could not be resolved; retry later") from None
    try:
        run_id = IngestionJobs(store.session_factory).enqueue(repo.id, target,
            branch=repo.tracked_branch, installation_id=repo.installation_id,
            trigger=trigger, force_full=force_full, delivery_id=delivery_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    tasks.add_task(dispatch_one, run_id)
    return run_id
