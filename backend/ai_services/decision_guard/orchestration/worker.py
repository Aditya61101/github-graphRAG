"""One worker by durable job ID, shared by BackgroundTasks and Cloud Run Jobs."""

import asyncio
import logging
from contextlib import suppress

from ai_services.decision_guard.context import (
    ContextConstructionError,
    conservative_tokens,
)
from ai_services.decision_guard.evaluation.models import (
    EvaluationResult,
    EvaluationSettings,
)
from ai_services.decision_guard.models import ArchitectureContext
from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.pr.models import (
    PullRequestChangeSet,
    PullRequestEvent,
    SupersededRevisionError,
)

from .packets import DurableEvaluationModel
from .store import LostLease
from .check_lifecycle import enabled as lifecycle_enabled, queue_max_age
from shared.utils.processing import background_processing_enabled

logger = logging.getLogger(__name__)


class EvaluationWorker:
    def __init__(self, store, runtime, *, concurrency=2, publisher=None, runtime_factory=None):
        self.store, self.runtime = store, runtime
        if lifecycle_enabled():
            queue_max_age()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.publisher, self.runtime_factory = publisher, runtime_factory

    async def _publish_check(self, job_id):
        try:
            await self.publisher.publish(job_id)
        except Exception as error:  # publication must not change evaluation outcome
            logger.warning('Check delivery deferred job=%s error_type=%s', job_id, type(error).__name__)

    async def _current(self, job, token, event, runtime=None):
        current = await (runtime or self.runtime).current_event(event)
        if current is None:
            raise SupersededRevisionError("Pull request is no longer open")
        if current.revision_key != event.revision_key:
            self.store.enqueue(
                job.repository_id,
                current,
                EvaluationSettings.model_validate_json(job.settings_json),
                observed_by=(job.id, token),
            )
            raise SupersededRevisionError("GitHub PR base/head has advanced")
        self.store.observe_current(job.id, token)

    async def _heartbeat(self, job_id, token, execution):
        try:
            while True:
                await asyncio.sleep(max(0.05, self.store.lease_seconds / 3))
                self.store.heartbeat(job_id, token)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- any DB ownership failure must stop execution
            # Stop dispatch/source work immediately if DB ownership is uncertain.
            execution.cancel()
            logger.warning(
                "Evaluation heartbeat failed job=%s error_type=%s",
                job_id,
                type(error).__name__,
            )

    async def _execute(self, job, token):
        if self.runtime_factory is not None:
            async with self.runtime_factory() as runtime:
                return await self._execute_runtime(job, token, runtime)
        return await self._execute_runtime(job, token, self.runtime)

    async def _execute_runtime(self, job, token, runtime):
        event = PullRequestEvent.model_validate_json(job.event_json)
        await self._current(job, token, event, runtime)
        # A durable final-stage checkpoint needs no checkout or model execution.
        if job.result_json and job.stage == "EVALUATED":
            result = EvaluationResult.model_validate_json(job.result_json)
        else:
            async with runtime.open(job, event) as pipeline:
                if job.changes_json:
                    changes = PullRequestChangeSet.model_validate_json(job.changes_json)
                else:
                    changes = await pipeline.ingest()
                    if (
                        changes.revision_key != job.revision_key
                        or changes.repository_id != job.repository_id
                    ):
                        raise ValueError("Ingested PR revision mismatch")
                    self.store.checkpoint(
                        job.id,
                        token,
                        "INGESTED",
                        changes_json=changes.model_dump_json(),
                    )
                if job.context_json:
                    context = ArchitectureContext.model_validate_json(job.context_json)
                else:
                    context = await pipeline.context(changes)
                    self.store.checkpoint(
                        job.id,
                        token,
                        "CONTEXT_BUILT",
                        context_json=context.model_dump_json(),
                    )
                settings = EvaluationSettings.model_validate_json(job.settings_json)
                model = DurableEvaluationModel(
                    runtime.model,
                    self.store,
                    job.id,
                    token,
                    settings,
                    counter=getattr(pipeline, "token_counter", conservative_tokens),
                )
                result = await pipeline.evaluate(context, model)
                # Validate before any persistence, including final source/citation gates.
                result = EvaluationResult.model_validate_json(result.model_dump_json())
                self.store.checkpoint(
                    job.id, token, "EVALUATED", result_json=result.model_dump_json()
                )
        # Remote freshness and installation authorization are checked again after
        # the expensive work. The DB pointer update is fenced in one transaction.
        await self._current(job, token, event, runtime)
        self.store.finish(job.id, token, result)

    async def run_job(self, job_id):
        if not background_processing_enabled():
            return
        async with self.semaphore:
            token = self.store.claim(job_id)
            if token is None:
                return
            job = self.store.get(job_id)
            publication = (asyncio.create_task(self._publish_check(job_id))
                           if lifecycle_enabled() and self.publisher is not None else None)
            execution = asyncio.create_task(self._execute(job, token))
            heartbeat = asyncio.create_task(self._heartbeat(job_id, token, execution))
            try:
                await execution
            except LostLease:
                logger.info("Evaluation lease lost job=%s", job_id)
            except asyncio.CancelledError:
                # Leave the persisted lease/reservations for recovery. Unknown
                # in-flight model calls are never automatically dispatched again.
                raise
            except Exception as error:  # noqa: BLE001 -- persist safe dependency failure metadata
                code = type(error).__name__
                retryable = True
                if isinstance(error, SupersededRevisionError):
                    code, retryable = "SUPERSEDED", False
                elif isinstance(error, GitHubAppError):
                    code = f"GITHUB_{error.status_code}"
                    retryable = error.status_code == 429 or error.status_code >= 500
                elif isinstance(error, ValueError):
                    retryable = False
                message = (
                    "Baseline must be COMPLETED at this PR base SHA with no unfinished ingestion; sync the repository and retry."
                    if isinstance(error, ContextConstructionError)
                    else None
                )
                try:
                    self.store.fail(
                        job_id, token, code, retryable=retryable, message=message
                    )
                except LostLease:
                    logger.info(
                        "Failure recording skipped after lease loss job=%s", job_id
                    )
                # Provider/source exception strings may contain credentials/source.
                logger.warning(
                    "Evaluation failed job=%s stage=%s error_type=%s",
                    job_id,
                    self.store.get(job_id).stage,
                    code,
                )
            finally:
                if publication is not None:
                    if not publication.done():
                        publication.cancel()
                    with suppress(asyncio.CancelledError):
                        await publication
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if lifecycle_enabled() and self.publisher is not None:
                # Result/failure intent may have advanced while progress HTTP was
                # in flight; use the same outbox/identity, never a second Check.
                await self._publish_check(job_id)
