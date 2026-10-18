"""Short, owned transactions with compare-and-swap claims and fencing tokens."""

from __future__ import annotations

import json
import os
import time
import uuid

from sqlalchemy import and_, func, or_, select, text, update

from ai_services.decision_guard.evaluation.models import (
    EvaluationResult,
    EvaluationSettings,
)
from ai_services.ingestion.persistence.models import RepositoryModel

from . import read_models
from .check_lifecycle import enabled as lifecycle_enabled, intent as lifecycle_intent, queue_max_age
from .models import (
    DispatchIntent,
    EvaluationAttempt,
    EvaluationJob,
    ModelPacket,
    PullRequestEvaluationHead,
    WebhookDelivery,
)


class LostLease(RuntimeError):
    pass


class EvaluationStore:
    def __init__(self, session_factory, *, clock=time.time, lease_seconds=120):
        self.sessions, self.clock, self.lease_seconds = (
            session_factory,
            clock,
            lease_seconds,
        )

    @staticmethod
    def _repository_lock(session, repository_id):
        # A harmless UPDATE obtains a database write lock on SQLite and a row
        # lock on PostgreSQL. All PR pointer writers take locks in this order.
        if (
            session.execute(
                update(RepositoryModel)
                .where(RepositoryModel.id == repository_id)
                .values(id=RepositoryModel.id, updated_at=RepositoryModel.updated_at)
            ).rowcount
            != 1
        ):
            raise ValueError("Tracked repository no longer exists")

    def enqueue(self, repository_id, event, settings=None, *, observed_by=None):
        now = self.clock()
        settings = settings or EvaluationSettings()
        with self.sessions.begin() as session:
            self._repository_lock(session, repository_id)
            if observed_by is not None:
                self._fence(session, *observed_by)
            if event.delivery_id:
                delivery = session.get(WebhookDelivery, event.delivery_id)
                if delivery:
                    job = session.get(EvaluationJob, delivery.job_id)
                    if (
                        job.repository_id != repository_id
                        or job.revision_key != event.revision_key
                    ):
                        raise ValueError(
                            "Webhook delivery identity was reused with different metadata"
                        )
                    if lifecycle_enabled():
                        lifecycle_intent(session, job, now)
                    return job.id
            job = session.scalar(
                select(EvaluationJob).where(
                    EvaluationJob.repository_id == repository_id,
                    EvaluationJob.pr_number == event.pull_request_number,
                    EvaluationJob.revision_key == event.revision_key,
                )
            )
            if job is None:
                job = EvaluationJob(
                    id=uuid.uuid4().hex,
                    repository_id=repository_id,
                    pr_number=event.pull_request_number,
                    revision_key=event.revision_key,
                    base_sha=event.base_sha,
                    head_sha=event.head_sha,
                    event_json=event.model_dump_json(),
                    settings_json=settings.model_dump_json(),
                    status="QUEUED",
                    stage="QUEUED",
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
                session.flush()
            elif job.status == "SUPERSEDED" and observed_by is None:
                current = session.get(
                    read_models.PullRequestState,
                    (repository_id, event.pull_request_number),
                )
                if (
                    current
                    and current.state == "open"
                    and current.freshness == "OBSERVED"
                    and (current.base_sha, current.head_sha)
                    == (event.base_sha, event.head_sha)
                ):
                    # Only one of the existing signed analysis actions can revive
                    # this revision. Lifecycle synchronization never queues AI.
                    job.status, job.next_attempt_at = "QUEUED", now
                    job.max_attempts = max(job.max_attempts, job.attempts + 1)
                    if not (job.result_json and job.stage == "EVALUATED"):
                        job.settings_json = settings.model_dump_json()
            head = session.get(
                PullRequestEvaluationHead, (repository_id, event.pull_request_number)
            )
            # Redelivery of an already-known revision cannot roll the head back.
            # Live GitHub reconciliation, rather than delivery arrival order,
            # establishes freshness at execution and publication time.
            if head is None:
                session.add(
                    PullRequestEvaluationHead(
                        repository_id=repository_id,
                        pr_number=event.pull_request_number,
                        latest_job_id=job.id,
                    )
                )
            elif job.status == "QUEUED" or observed_by is not None:
                self._advance(session, head, job.id, now)
                if observed_by is not None and job.status == "SUPERSEDED":
                    job.status, job.next_attempt_at = "QUEUED", now
                    job.max_attempts = max(job.max_attempts, job.attempts + 1)
                if (
                    observed_by is not None
                    and job.result_json
                    and job.status in {"COMPLETED", "DEFERRED"}
                ):
                    head.current_result_job_id = job.id
            if event.delivery_id:
                session.add(
                    WebhookDelivery(
                        delivery_id=event.delivery_id, job_id=job.id, received_at=now
                    )
                )
            if job.result_json and job.stage == "EVALUATED":
                current = session.get(
                    read_models.PullRequestState,
                    (repository_id, event.pull_request_number),
                )
                if (
                    current
                    and current.state == "open"
                    and current.freshness == "OBSERVED"
                    and (current.base_sha, current.head_sha)
                    == (event.base_sha, event.head_sha)
                ):
                    from .projections import queue_publication

                    queue_publication(
                        session,
                        job,
                        now,
                        EvaluationResult.model_validate_json(
                            job.result_json
                        ).evaluation_id,
                    )
            if (
                job.status in {"QUEUED", "RETRY"}
                and session.get(DispatchIntent, job.id) is None
            ):
                session.add(DispatchIntent(job_id=job.id, next_dispatch_at=now))
            if lifecycle_enabled():
                lifecycle_intent(session, job, now)
            return job.id

    @staticmethod
    def _advance(session, head, job_id, now):
        if head.latest_job_id == job_id:
            return
        retiring = []
        if lifecycle_enabled():
            retiring = list(session.scalars(select(EvaluationJob).where(
                EvaluationJob.repository_id == head.repository_id,
                EvaluationJob.pr_number == head.pr_number,
                EvaluationJob.id != job_id,
                EvaluationJob.status.in_(['QUEUED', 'RETRY', 'RUNNING', 'DEFERRED']),
            )))
        session.execute(
            update(EvaluationJob)
            .where(
                EvaluationJob.repository_id == head.repository_id,
                EvaluationJob.pr_number == head.pr_number,
                EvaluationJob.id != job_id,
                EvaluationJob.status.in_(["QUEUED", "RETRY", "RUNNING", "DEFERRED"]),
            )
            .values(
                status="SUPERSEDED", lease_token=None, lease_until=None, updated_at=now
            )
        )
        session.execute(
            update(EvaluationAttempt)
            .where(
                EvaluationAttempt.job_id == head.latest_job_id,
                EvaluationAttempt.status == "RUNNING",
            )
            .values(status="SUPERSEDED", finished_at=now)
        )
        head.latest_job_id, head.current_result_job_id = job_id, None
        for old in retiring:
            session.refresh(old)
            lifecycle_intent(session, old, now)

    def observe_current(self, job_id, token):
        """Called only after GitHub confirms this exact open PR revision."""
        now = self.clock()
        with self.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            self._repository_lock(session, job.repository_id)
            self._fence(session, job_id, token)
            head = session.get(
                PullRequestEvaluationHead, (job.repository_id, job.pr_number)
            )
            self._advance(session, head, job_id, now)

    def get(self, job_id):
        with self.sessions() as session:
            return session.get(EvaluationJob, job_id)

    def list(self, repository_id, pr_number=None, limit=50, offset=0):
        with self.sessions() as session:
            query = select(EvaluationJob).where(
                EvaluationJob.repository_id == repository_id
            )
            if pr_number is not None:
                query = query.where(EvaluationJob.pr_number == pr_number)
            return list(
                session.scalars(
                    query.order_by(EvaluationJob.created_at.desc(), EvaluationJob.id)
                    .limit(limit)
                    .offset(offset)
                )
            )

    def claim(self, job_id):
        now, token = self.clock(), uuid.uuid4().hex
        with self.sessions.begin() as session:
            if lifecycle_enabled():
                self._expire_waiting(session, now, job_id=job_id)
            limit = int(os.getenv("PR_EVALUATION_MAX_CONCURRENCY", "2"))
            if limit < 1:
                raise ValueError("PR_EVALUATION_MAX_CONCURRENCY must be positive")
            # Serialize admission across instances and revisions. Only short SQL
            # work holds this lock; execution remains protected by job leases.
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(4920260304)"))
            else:
                session.execute(text("BEGIN IMMEDIATE"))
            active = session.scalar(select(func.count()).select_from(EvaluationJob).where(
                EvaluationJob.status == "RUNNING", EvaluationJob.lease_until > now))
            if active >= limit:
                return None
            eligible = or_(
                and_(
                    EvaluationJob.status.in_(["QUEUED", "RETRY"]),
                    EvaluationJob.next_attempt_at <= now,
                ),
                and_(
                    EvaluationJob.status == "RUNNING", EvaluationJob.lease_until <= now
                ),
            )
            changed = session.execute(
                update(EvaluationJob)
                .where(
                    EvaluationJob.id == job_id,
                    eligible,
                    or_(
                        EvaluationJob.attempts < EvaluationJob.max_attempts,
                        and_(
                            EvaluationJob.stage == "EVALUATED",
                            EvaluationJob.result_json.is_not(None),
                        ),
                    ),
                )
                .values(
                    status="RUNNING",
                    lease_token=token,
                    lease_until=now + self.lease_seconds,
                    attempts=EvaluationJob.attempts + 1,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                return None
            session.execute(
                update(EvaluationAttempt)
                .where(
                    EvaluationAttempt.job_id == job_id,
                    EvaluationAttempt.status == "RUNNING",
                )
                .values(status="LEASE_EXPIRED", finished_at=now)
            )
            job = session.get(EvaluationJob, job_id)
            session.add(
                EvaluationAttempt(
                    token=token,
                    job_id=job_id,
                    number=job.attempts,
                    started_at=now,
                    status="RUNNING",
                )
            )
            if lifecycle_enabled():
                lifecycle_intent(session, job, now)
            return token

    def _fence(self, session, job_id, token, **values):
        now = self.clock()
        changed = session.execute(
            update(EvaluationJob)
            .where(
                EvaluationJob.id == job_id,
                EvaluationJob.status == "RUNNING",
                EvaluationJob.lease_token == token,
                EvaluationJob.lease_until > now,
            )
            .values(updated_at=now, **values)
        )
        if changed.rowcount != 1:
            raise LostLease("Evaluation execution lease is no longer owned")

    def heartbeat(self, job_id, token):
        with self.sessions.begin() as session:
            self._fence(
                session, job_id, token, lease_until=self.clock() + self.lease_seconds
            )

    def checkpoint(self, job_id, token, stage, **values):
        if set(values) - {"changes_json", "context_json", "result_json"}:
            raise ValueError("Unknown checkpoint column")
        with self.sessions.begin() as session:
            self._fence(session, job_id, token, stage=stage, **values)

    def finish(self, job_id, token, result):
        result = EvaluationResult.model_validate_json(result.model_dump_json())
        now = self.clock()
        deferred = [
            u.model_dump(mode="json")
            for u in result.units
            if u.status in {"ERROR", "BUDGET_OMITTED"}
        ]
        deferred += [
            {
                "unit_id": f.unit_id,
                "finding_id": f.finding_id,
                "reason_codes": [f.review_error],
            }
            for f in result.findings
            if f.review_error
        ]
        deferred += [
            {
                "reason_codes": [l.code],
                "message": l.message,
                "item_ids": list(l.item_ids),
            }
            for l in result.limitations
            if l.code.endswith("_LIMIT")
            or l.code
            in {
                "EVALUATION_BUDGET",
                "PREPARATION_DEADLINE",
                "REQUEST_EVIDENCE_GROUP_OMITTED",
                "MINIMUM_REQUEST_EVIDENCE_OMITTED",
            }
        ]
        with self.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            self._repository_lock(session, job.repository_id)
            if (
                result.revision.revision_key,
                result.revision.repository_id,
                result.revision.pull_request_number,
                result.revision.base_sha,
                result.revision.head_sha,
            ) != (
                job.revision_key,
                job.repository_id,
                job.pr_number,
                job.base_sha,
                job.head_sha,
            ):
                raise ValueError("Evaluation result job/revision mismatch")
            head = session.get(
                PullRequestEvaluationHead, (job.repository_id, job.pr_number)
            )
            status = "DEFERRED" if deferred else "COMPLETED"
            if head.latest_job_id != job_id:
                status = "SUPERSEDED"
            self._fence(
                session,
                job_id,
                token,
                status=status,
                stage="EVALUATED",
                result_json=result.model_dump_json(),
                deferred_json=json.dumps(deferred),
                lease_token=None,
                lease_until=None,
                error_code=None,
                error_message=None,
            )
            session.execute(
                update(EvaluationAttempt)
                .where(EvaluationAttempt.token == token)
                .values(status=status, finished_at=now)
            )
            if status != "SUPERSEDED":
                head.current_result_job_id = job_id
            from .projections import project

            project(session, job, result, now)
            intent = session.get(DispatchIntent, job_id)
            if intent:
                session.delete(intent)

    def fail(self, job_id, token, code, *, retryable=True, message=None):
        now = self.clock()
        with self.sessions.begin() as session:
            self._fence(session, job_id, token)
            job = session.get(EvaluationJob, job_id)
            status = (
                "RETRY" if retryable and job.attempts < job.max_attempts else "FAILED"
            )
            if code == "SUPERSEDED":
                status = "SUPERSEDED"
            job.status, job.lease_token, job.lease_until = status, None, None
            job.error_code = code[:100]
            job.error_message = (
                message
                or "Execution failed; inspect stage and error_code, resolve the dependency, then retry."
            )
            job.next_attempt_at = now + min(300, 2**job.attempts)
            if lifecycle_enabled():
                lifecycle_intent(session, job, now)
            if job.result_json and job.stage == "EVALUATED":
                from .projections import project

                project(
                    session,
                    job,
                    EvaluationResult.model_validate_json(job.result_json),
                    now,
                )
            if status == "FAILED" and not job.result_json:
                from .projections import queue_publication

                queue_publication(session, job, now)
            session.execute(
                update(EvaluationAttempt)
                .where(EvaluationAttempt.token == token)
                .values(status=status, finished_at=now, error_code=code[:100])
            )
            intent = session.get(DispatchIntent, job_id)
            if intent:
                if status == "RETRY":
                    intent.next_dispatch_at = job.next_attempt_at
                else:
                    session.delete(intent)

    def due(self, limit=50):
        """Recover missed dispatch, crashed execution and exhausted expired claims."""
        now = self.clock()
        with self.sessions.begin() as session:
            if lifecycle_enabled():
                self._expire_waiting(session, now)
            expired = and_(
                EvaluationJob.status == "RUNNING", EvaluationJob.lease_until <= now
            )
            exhausted = list(
                session.scalars(
                    select(EvaluationJob).where(
                        expired,
                        EvaluationJob.attempts >= EvaluationJob.max_attempts,
                        or_(
                            EvaluationJob.stage != "EVALUATED",
                            EvaluationJob.result_json.is_(None),
                        ),
                    )
                )
            )
            for job in exhausted:
                self._fence_expired(session, job, now)
            return list(
                session.scalars(
                    select(EvaluationJob.id)
                    .outerjoin(
                        DispatchIntent, DispatchIntent.job_id == EvaluationJob.id
                    )
                    .where(
                        or_(
                            expired,
                            and_(
                                EvaluationJob.status.in_(["QUEUED", "RETRY"]),
                                EvaluationJob.next_attempt_at <= now,
                            ),
                        ),
                        or_(
                            DispatchIntent.job_id.is_(None),
                            DispatchIntent.next_dispatch_at <= now,
                        ),
                    )
                    .order_by(EvaluationJob.created_at)
                    .limit(limit)
                )
            )

    @staticmethod
    def _expire_waiting(session, now, *, job_id=None):
        age = queue_max_age()
        waiting = or_(EvaluationJob.status.in_(['QUEUED', 'RETRY']), and_(
            EvaluationJob.status == 'RUNNING', EvaluationJob.lease_until <= now))
        filters = [waiting, EvaluationJob.created_at <= now - age,
                   or_(EvaluationJob.stage != 'EVALUATED', EvaluationJob.result_json.is_(None))]
        if job_id is not None:
            filters.append(EvaluationJob.id == job_id)
        for job in session.scalars(select(EvaluationJob).where(*filters)).all():
            changed = session.execute(update(EvaluationJob).where(
                EvaluationJob.id == job.id, *filters).values(
                status='FAILED', error_code='QUEUE_AGE_EXCEEDED',
                error_message='Evaluation could not start or recover before its queue deadline.',
                lease_token=None, lease_until=None, updated_at=now))
            if changed.rowcount:
                session.execute(update(EvaluationAttempt).where(
                    EvaluationAttempt.job_id == job.id, EvaluationAttempt.status == 'RUNNING'
                ).values(status='LEASE_EXPIRED', finished_at=now))
                intent = session.get(DispatchIntent, job.id)
                if intent:
                    session.delete(intent)
                lifecycle_intent(session, job, now)

    def reconcile_check_lifecycle(self):
        """Repair intents from compatibility-mode workers; never schedule AI."""
        if not lifecycle_enabled():
            return
        publication = read_models.CheckPublication
        with self.sessions.begin() as session:
            terminal = ~EvaluationJob.status.in_(['QUEUED', 'RETRY', 'RUNNING'])
            jobs = list(session.scalars(select(EvaluationJob).outerjoin(
                publication, publication.job_id == EvaluationJob.id).where(or_(
                    and_(publication.job_id.is_(None), ~terminal),
                    and_(publication.desired_version == 0, ~terminal),
                    and_(publication.desired_state == 'queued', EvaluationJob.status.in_(['RUNNING', 'RETRY'])),
                    and_(publication.desired_state.in_(['queued', 'in_progress']), terminal),
                )).order_by(EvaluationJob.created_at).limit(200)))
            for job in jobs:
                evaluation_id = (EvaluationResult.model_validate_json(job.result_json).evaluation_id
                                 if job.result_json and job.stage == 'EVALUATED' and
                                 job.status not in {'QUEUED', 'RETRY', 'RUNNING'} else None)
                lifecycle_intent(session, job, self.clock(), evaluation_id=evaluation_id)

    @staticmethod
    def _fence_expired(session, job, now):
        changed = session.execute(
            update(EvaluationJob)
            .where(
                EvaluationJob.id == job.id,
                EvaluationJob.status == "RUNNING",
                EvaluationJob.lease_until <= now,
            )
            .values(
                status="FAILED",
                error_code="ATTEMPTS_EXHAUSTED",
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
        )
        if changed.rowcount:
            from .projections import queue_publication

            queue_publication(session, job, now)
            session.execute(
                update(EvaluationAttempt)
                .where(
                    EvaluationAttempt.job_id == job.id,
                    EvaluationAttempt.status == "RUNNING",
                )
                .values(status="LEASE_EXPIRED", finished_at=now)
            )

    def dispatched(self, job_id, retry_seconds=30):
        with self.sessions.begin() as session:
            session.execute(
                update(DispatchIntent)
                .where(DispatchIntent.job_id == job_id)
                .values(
                    next_dispatch_at=self.clock() + retry_seconds,
                    dispatch_count=DispatchIntent.dispatch_count + 1,
                )
            )

    def cached_packet(self, job_id, token, key):
        with self.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            self._repository_lock(session, job.repository_id)
            self._fence(session, job_id, token)
            self._active_adr_sources(session, job)
            packet = session.get(ModelPacket, (job_id, key))
            return packet.reply_json if packet and packet.state == "COMPLETED" else None

    @staticmethod
    def _active_adr_sources(session, job, request_json=None):
        from ai_services.ingestion.persistence.models import ADRModel
        context = json.loads(job.context_json) if job.context_json else {}
        ids = {d['adr_id'] for d in context.get('adr_snapshot', [])}
        if request_json:
            request = json.loads(request_json)
            request = request.get('assessment', request)
            ids.update(e['adr_id'] for e in request.get('evidence', []) if e.get('adr_id'))
        if session.scalar(select(ADRModel.id).where(
            ADRModel.repository_id == job.repository_id, ADRModel.id.in_(ids),
            ADRModel.status == 'DELETED').limit(1)):
            raise ValueError('Evaluation context contains a deleted ADR; rebuild context')

    def reserve_packet(self, job_id, token, key, phase, request_json, tokens, cost):
        """Reserve before dispatch. Uncertain calls remain charged at their ceiling."""
        with self.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            self._repository_lock(session, job.repository_id)
            self._fence(session, job_id, token)
            self._active_adr_sources(session, job, request_json)
            settings = EvaluationSettings.model_validate_json(job.settings_json)
            packet = session.get(ModelPacket, (job_id, key))
            if packet and packet.state == "IN_FLIGHT":
                # Do not silently issue a second potentially billed call.
                return "UNKNOWN_PROVIDER_CALL"
            if (
                job.provider_attempts >= settings.max_attempts
                or job.charged_tokens + tokens > settings.max_tokens
                or (
                    settings.max_cost is not None
                    and job.charged_cost + cost > settings.max_cost
                )
            ):
                return "BUDGET_DENIED"
            if packet is None:
                packet = ModelPacket(
                    job_id=job_id,
                    packet_key=key,
                    phase=phase,
                    request_json=request_json,
                )
                session.add(packet)
            packet.state, packet.lease_token = "IN_FLIGHT", token
            packet.reply_json = None
            packet.reserved_tokens, packet.reserved_cost, packet.updated_at = (
                tokens,
                cost,
                self.clock(),
            )
            job.provider_attempts += 1
            job.charged_tokens += tokens
            job.charged_cost += cost
            return None

    def save_packet(self, job_id, token, key, reply, state):
        with self.sessions.begin() as session:
            self._fence(session, job_id, token)
            packet = session.get(ModelPacket, (job_id, key))
            if packet.lease_token != token or packet.state != "IN_FLIGHT":
                raise LostLease("Packet ownership changed")
            packet.reply_json, packet.state, packet.updated_at = (
                reply.model_dump_json() if reply else None,
                state,
                self.clock(),
            )
            # Refund only reported usage. Unknown usage keeps the full reserve.
            if reply:
                job = session.get(EvaluationJob, job_id)
                settings = EvaluationSettings.model_validate_json(job.settings_json)
                if reply.input_tokens is not None and reply.output_tokens is not None:
                    actual = reply.input_tokens + reply.output_tokens
                    job.charged_tokens += actual - packet.reserved_tokens
                    actual_cost = (
                        reply.input_tokens * (settings.input_rate_per_million or 0)
                        + reply.output_tokens * (settings.output_rate_per_million or 0)
                    ) / 1_000_000
                    job.charged_cost += actual_cost - packet.reserved_cost

    def void_packet(self, job_id, token, key):
        """Refund only an explicit provider-boundary pre-dispatch denial."""
        with self.sessions.begin() as session:
            self._fence(session, job_id, token)
            packet = session.get(ModelPacket, (job_id, key))
            if packet.lease_token != token or packet.state != "IN_FLIGHT":
                raise LostLease("Packet ownership changed")
            job = session.get(EvaluationJob, job_id)
            job.provider_attempts -= 1
            job.charged_tokens -= packet.reserved_tokens
            job.charged_cost -= packet.reserved_cost
            packet.state, packet.updated_at = "DENIED", self.clock()

    def diagnostics(self, job_id):
        with self.sessions() as session:
            job = session.get(EvaluationJob, job_id)
            attempts = list(
                session.scalars(
                    select(EvaluationAttempt)
                    .where(EvaluationAttempt.job_id == job_id)
                    .order_by(EvaluationAttempt.number)
                )
            )
            packets = list(
                session.scalars(select(ModelPacket).where(ModelPacket.job_id == job_id))
            )
            head = session.get(
                PullRequestEvaluationHead, (job.repository_id, job.pr_number)
            )
            return {
                "is_current_observed_result": head.current_result_job_id == job_id,
                "provider_attempts": job.provider_attempts,
                "charged_tokens": job.charged_tokens,
                "charged_cost": job.charged_cost,
                "deferred": json.loads(job.deferred_json),
                "attempts": [
                    {
                        "number": a.number,
                        "status": a.status,
                        "started_at": a.started_at,
                        "finished_at": a.finished_at,
                        "error_code": a.error_code,
                    }
                    for a in attempts
                ],
                "packets": [
                    {
                        "key": p.packet_key,
                        "phase": p.phase,
                        "state": p.state,
                        "reserved_tokens": p.reserved_tokens,
                    }
                    for p in packets
                ],
            }

    def continue_job(
        self,
        job_id,
        *,
        settings=None,
        acknowledge_unknown_calls=False,
        rebuild_context=False,
    ):
        """An authenticated, explicit continuation may raise lifetime ceilings.

        Completed exact packets and prior spending are retained. Acknowledging
        unknown calls accepts a possible duplicate charge, not exactly-once API
        execution. Previous reservations are never refunded by acknowledgement.
        """
        now = self.clock()
        with self.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            self._repository_lock(session, job.repository_id)
            # Serialize against any claim/finalizer; status is re-read after lock.
            session.refresh(job)
            if job.status not in {"FAILED", "DEFERRED"}:
                raise ValueError(
                    "Only failed or deferred current runs can be continued"
                )
            head = session.get(
                PullRequestEvaluationHead, (job.repository_id, job.pr_number)
            )
            if head.latest_job_id != job_id:
                raise ValueError(
                    "This evaluation was superseded by a different revision"
                )
            if settings:
                previous = EvaluationSettings.model_validate_json(job.settings_json)
                # These fields affect context pruning, source preparation or the
                # coherent singleton feasibility probe. Reusing that checkpoint
                # after a change would preserve omissions from the old profile.
                context_fields = (
                    "token_encoding", "max_input_tokens", "max_output_tokens",
                    "model_window", "provider_reserve", "max_batch_units", "max_units",
                    "max_reads", "max_read_bytes", "max_source_bytes",
                    "max_policy_reads", "max_policy_read_bytes", "max_policy_bytes",
                )
                if job.context_json and not rebuild_context and any(
                    getattr(previous, name) != getattr(settings, name)
                    for name in context_fields
                ):
                    raise ValueError(
                        "Context-affecting settings changes require rebuild_context=True"
                    )
                job.settings_json = settings.model_dump_json()
            if rebuild_context:
                job.context_json = None
            if acknowledge_unknown_calls:
                session.execute(
                    update(ModelPacket)
                    .where(
                        ModelPacket.job_id == job_id, ModelPacket.state == "IN_FLIGHT"
                    )
                    .values(state="UNKNOWN_ACKNOWLEDGED")
                )
            job.status, job.stage = "QUEUED", "CONTINUING"
            job.max_attempts = job.attempts + 3
            job.next_attempt_at, job.updated_at = now, now
            job.error_code, job.error_message = None, None
            intent = session.get(DispatchIntent, job_id)
            if intent:
                intent.next_dispatch_at = now
            else:
                session.add(DispatchIntent(job_id=job_id, next_dispatch_at=now))
