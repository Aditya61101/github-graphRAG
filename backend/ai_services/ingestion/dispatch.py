"""Cloud Run Jobs launch transport, using existing orchestration dispatcher."""
import os
from ai_services.decision_guard.orchestration.dispatch import CloudRunJobsDispatcher


class IngestionJobsDispatcher(CloudRunJobsDispatcher):
    async def dispatch_adr(self, run_id):
        resource = os.getenv('ADR_INGESTION_JOB_RESOURCE')
        if not resource:
            raise RuntimeError('ADR_INGESTION_JOB_RESOURCE is required')
        await ADRJobsDispatcher(self.client, self.access_token, resource).dispatch(run_id)

    @staticmethod
    def payload(run_id):
        return {"overrides": {"containerOverrides": [{
            # Cloud Run's `args` override replaces arguments to the configured
            # container entrypoint; it does not replace the entrypoint itself.
            # The Job is configured with command `python`, so omit the binary
            # here to produce `python -m ai_services.runs.run_repository_ingestion`.
            "args": ["-m", "ai_services.runs.run_repository_ingestion"],
            "env": [{"name": "INGESTION_RUN_ID", "value": run_id}],
        }]}}


class ADRJobsDispatcher(CloudRunJobsDispatcher):
    @staticmethod
    def payload(run_id):
        return {"overrides": {"containerOverrides": [{
            "args": ["-m", "ai_services.runs.run_adr_ingestion"],
            "env": [{"name": "INGESTION_RUN_ID", "value": run_id}],
        }]}}


async def access_token():
    import asyncio
    def resolve():
        import google.auth
        from google.auth.transport.requests import Request
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        return credentials.token
    return await asyncio.to_thread(resolve)


def configured_dispatcher(client):
    resource = os.getenv("INGESTION_JOB_RESOURCE")
    if not resource:
        raise RuntimeError("INGESTION_JOB_RESOURCE must identify the baseline Cloud Run Job")
    return IngestionJobsDispatcher(client, access_token, resource)


async def reconcile(jobs, dispatcher):
    from shared.utils.processing import require_background_processing
    require_background_processing()
    for run_id in jobs.due():
        try:
            run = jobs.get(run_id)
            if run.trigger == 'adr':
                await dispatcher.dispatch_adr(run_id)
            else:
                await dispatcher.dispatch(run_id)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Ingestion dispatch deferred run=%s type=%s",
                                                run_id, type(exc).__name__)
