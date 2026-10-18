"""Durable execution is independent of the launch transport."""

import asyncio
import base64
import json
import logging
from typing import Protocol

from ai_services.decision_guard.evaluation.models import EvaluationSettings
from shared.utils.processing import background_processing_enabled as _processing_enabled
from .check_lifecycle import enabled as lifecycle_enabled, queue_max_age

logger = logging.getLogger(__name__)


class Dispatcher(Protocol):
    async def dispatch(self, job_id: str) -> None: ...


class LocalDispatcher:
    def __init__(self, worker, background_tasks=None, tasks=None):
        self.worker, self.background_tasks = worker, background_tasks
        self.tasks = tasks if tasks is not None else {}

    async def dispatch(self, job_id):
        if self.background_tasks is not None:
            self.background_tasks.add_task(self.worker.run_job, job_id)
        elif job_id not in self.tasks or self.tasks[job_id].done():
            task = asyncio.create_task(self.worker.run_job(job_id))
            self.tasks[job_id] = task

            def done(completed):
                if self.tasks.get(job_id) is completed:
                    self.tasks.pop(job_id, None)
                if not completed.cancelled() and completed.exception():
                    logger.error(
                        "Local evaluation task failed job=%s error_type=%s",
                        job_id,
                        type(completed.exception()).__name__,
                    )

            task.add_done_callback(done)


class CloudRunJobsDispatcher:
    """Injected HTTP client and async Google access-token resolver; no GCP SDK.

    resource: projects/PROJECT/locations/REGION/jobs/JOB
    The job runs `python -m ai_services.runs.run_pr_evaluation`.
    """

    def __init__(self, client, access_token, resource):
        if not resource.startswith("projects/") or "/jobs/" not in resource:
            raise ValueError("Expected a Cloud Run Job resource")
        self.client, self.access_token, self.resource = client, access_token, resource

    @staticmethod
    def payload(job_id):
        return {
            "overrides": {
                "containerOverrides": [
                    {"env": [{"name": "PR_EVALUATION_JOB_ID", "value": job_id}]}
                ]
            }
        }

    async def dispatch(self, job_id):
        response = await self.client.post(
            f"https://run.googleapis.com/v2/{self.resource}:run",
            headers={"Authorization": "Bearer " + await self.access_token()},
            json=self.payload(job_id),
        )
        response.raise_for_status()


class CloudTasksDispatcher:
    """Cloud Tasks HTTP task launches the same Cloud Run Job via its v2 API.

    Cloud Tasks uses OAuth (Google API target), not an unauthenticated public
    worker endpoint. Cloud IAM/service-account setup belongs to deployment.
    """

    def __init__(
        self, client, access_token, queue, run_resource, service_account_email
    ):
        self.client, self.access_token, self.queue = client, access_token, queue
        self.run_resource, self.service_account_email = (
            run_resource,
            service_account_email,
        )

    async def dispatch(self, job_id):
        body = base64.b64encode(
            json.dumps(CloudRunJobsDispatcher.payload(job_id)).encode()
        ).decode()
        response = await self.client.post(
            f"https://cloudtasks.googleapis.com/v2/{self.queue}/tasks",
            headers={"Authorization": "Bearer " + await self.access_token()},
            json={
                "task": {
                    "httpRequest": {
                        "httpMethod": "POST",
                        "url": f"https://run.googleapis.com/v2/{self.run_resource}:run",
                        "headers": {"Content-Type": "application/json"},
                        "body": body,
                        "oauthToken": {
                            "serviceAccountEmail": self.service_account_email,
                            "scope": "https://www.googleapis.com/auth/cloud-platform",
                        },
                    }
                }
            },
        )
        response.raise_for_status()


class EvaluationOrchestrator:
    def __init__(
        self,
        store,
        worker,
        *,
        settings=None,
        settings_factory=None,
        dispatcher=None,
        publisher=None,
        lifecycle_github=None,
        processing_enabled=None,
    ):
        self.store, self.worker = store, worker
        if lifecycle_enabled():
            queue_max_age()
        self.processing_enabled = processing_enabled or _processing_enabled
        self.publisher = publisher
        self.lifecycle_github = lifecycle_github
        if settings is not None and settings_factory is not None:
            raise ValueError("Provide settings or settings_factory, not both")
        self.settings = settings or EvaluationSettings()
        self.settings_factory = settings_factory
        self.tasks = {}
        self.dispatcher = dispatcher or LocalDispatcher(worker, tasks=self.tasks)
        if worker is not None and publisher is not None:
            worker.publisher = publisher

    async def accept(self, repository_id, event, background_tasks):
        if not self.processing_enabled():
            raise RuntimeError("PR evaluation processing is disabled")
        settings = self.settings_factory() if self.settings_factory else self.settings
        job_id = self.store.enqueue(repository_id, event, settings)
        if lifecycle_enabled() and self.publisher is not None:
            background_tasks.add_task(self._accepted_with_check, job_id)
        elif isinstance(self.dispatcher, LocalDispatcher):
            await LocalDispatcher(self.worker, background_tasks).dispatch(job_id)
        else:
            # Cloud API latency must not delay the persisted webhook response.
            background_tasks.add_task(self._dispatch_persisted, job_id)
        return job_id

    async def _accepted_with_check(self, job_id):
        # GitHub must not gate dispatch (and neither result may cancel the other).
        launch = (self.worker.run_job(job_id) if isinstance(self.dispatcher, LocalDispatcher)
                  else self._dispatch_persisted(job_id))
        results = await asyncio.gather(self.publisher.publish(job_id), launch,
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning('Accepted evaluation recovery pending error_type=%s', type(result).__name__)

    async def _dispatch_persisted(self, job_id):
        if not self.processing_enabled():
            return
        try:
            await self.dispatcher.dispatch(job_id)
            self.store.dispatched(job_id)
        except Exception as error:  # noqa: BLE001 -- committed intent survives transport failure
            # A committed outbox remains recoverable; response is still accepted.
            logger.warning(
                "Evaluation dispatch deferred job=%s error_type=%s",
                job_id,
                type(error).__name__,
            )

    async def reconcile(self):
        if not self.processing_enabled():
            return
        jobs_due = None
        if lifecycle_enabled():
            # Expire abandoned jobs BEFORE publishing; compatibility workers may
            # also have changed a job without emitting a lifecycle intent.
            jobs_due = self.store.due()
            self.store.reconcile_check_lifecycle()
        if self.lifecycle_github is not None:
            from .projections import synchronize_due

            try:
                await synchronize_due(self.store, self.lifecycle_github)
            except Exception as error:  # noqa: BLE001 -- independently recover lifecycle and evaluation
                logger.warning(
                    "PR metadata recovery unavailable error_type=%s",
                    type(error).__name__,
                )
        if self.publisher is not None:
            try:
                await self.publisher.reconcile()
            except Exception as error:  # noqa: BLE001 -- delivery must not block evaluation dispatch
                logger.warning(
                    "Check publication recovery unavailable error_type=%s",
                    type(error).__name__,
                )
        for job_id in self.store.due() if jobs_due is None else jobs_due:
            try:
                await self.dispatcher.dispatch(job_id)
                self.store.dispatched(job_id)
            except Exception as error:  # noqa: BLE001 -- isolate dispatch failures by durable job
                logger.warning(
                    "Evaluation reconciliation failed job=%s error_type=%s",
                    job_id,
                    type(error).__name__,
                )

    async def recover_forever(self, interval=5):
        while True:
            try:
                await self.reconcile()
            except Exception as error:  # noqa: BLE001 -- recovery survives transient DB failures
                logger.warning(
                    "Evaluation recovery unavailable error_type=%s",
                    type(error).__name__,
                )
            await asyncio.sleep(interval)

    async def close(self):
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
