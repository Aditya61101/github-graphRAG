"""Durable repository ingestion using existing run history and short SQL transactions."""
import re
import time
import uuid
from sqlalchemy import select, update, or_, text
from .models import (
    RepositoryModel, IngestionRunModel, RepositoryIngestionState as State,
    IngestionDelivery, utc_now,
)


class LostIngestionLease(RuntimeError):
    pass


class IngestionJobs:
    def __init__(self, sessions, *, clock=time.time, lease_seconds=120, max_attempts=3):
        self.sessions, self.clock = sessions, clock
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts

    def _now(self, session):
        if self.clock is time.time and session.get_bind().dialect.name == "postgresql":
            return int(session.scalar(text("SELECT extract(epoch FROM clock_timestamp())")))
        return int(self.clock())

    @staticmethod
    def _lock(s, repo_id):
        # Stable lock order: repository, state, run. Also serializes SQLite tests.
        if s.execute(update(RepositoryModel).where(RepositoryModel.id == repo_id)
                     .values(id=RepositoryModel.id, updated_at=RepositoryModel.updated_at)).rowcount != 1:
            raise ValueError("Unknown repository")
        state = s.get(State, repo_id)
        if state is None:
            state = State(repository_id=repo_id, epoch=0, state="UNVERIFIED")
            s.add(state)
            s.flush()
        return state

    def enqueue(self, repo_id, target, *, branch, installation_id, trigger="manual",
                force_full=False, delivery_id=None):
        if not re.fullmatch("[0-9a-f]{40}", target):
            raise ValueError("An exact target commit SHA is required")
        now = int(self.clock())
        with self.sessions.begin() as s:
            state = self._lock(s, repo_id)
            now = self._now(s)
            repo = s.get(RepositoryModel, repo_id)
            if repo.installation_id != str(installation_id) or repo.tracked_branch != branch:
                raise ValueError("Repository installation or branch changed")
            if delivery_id:
                previous = s.get(IngestionDelivery, delivery_id)
                if previous:
                    if (previous.repository_id, previous.target_sha) != (repo_id, target):
                        raise ValueError("Webhook delivery identity mismatch")
                    return previous.run_id
            # Coalesce duplicate targets while preserving manual retries of failed runs.
            run = s.scalar(select(IngestionRunModel).where(
                IngestionRunModel.repository_id == repo_id,
                IngestionRunModel.target_commit_sha == target,
                IngestionRunModel.status.in_(["QUEUED", "RETRY", "INDEXING"]),
                IngestionRunModel.force_full == int(force_full),
                IngestionRunModel.trigger == trigger,
            ).order_by(IngestionRunModel.started_at).limit(1))
            if run is None:
                run = IngestionRunModel(id="run_" + uuid.uuid4().hex,
                    repository_id=repo_id, target_commit_sha=target,
                    status="QUEUED", trigger=trigger, is_incremental=0,
                    force_full=int(force_full), branch=branch,
                    installation_id=str(installation_id), next_attempt_at=now)
                s.add(run)
                s.flush()
            if delivery_id:
                s.add(IngestionDelivery(delivery_id=delivery_id, repository_id=repo_id,
                    target_sha=target, run_id=run.id))
            return run.id

    def get(self, run_id):
        with self.sessions() as s:
            return s.get(IngestionRunModel, run_id)

    def due(self):
        now = int(self.clock())
        with self.sessions() as s:
            return list(s.scalars(select(IngestionRunModel.id).where(or_(
                (IngestionRunModel.status.in_(["QUEUED", "RETRY"])) &
                    (IngestionRunModel.next_attempt_at <= now),
                (IngestionRunModel.status == "INDEXING") &
                    (IngestionRunModel.lease_until <= now),
            )).order_by(IngestionRunModel.started_at).limit(100)))

    def claim(self, run_id):
        now = int(self.clock())
        with self.sessions.begin() as s:
            candidate = s.get(IngestionRunModel, run_id)
            if candidate is None:
                return None
            state = self._lock(s, candidate.repository_id)
            now = self._now(s)
            s.refresh(candidate)
            if candidate.status not in {"QUEUED", "RETRY", "INDEXING"}:
                return None
            if candidate.next_attempt_at > now:
                return None
            if state.active_run_id:
                active = s.get(IngestionRunModel, state.active_run_id)
                if active and active.lease_until and active.lease_until > now:
                    return None
                if active and active.id != run_id and active.status == "INDEXING":
                    active.status, active.completed_at = "FAILED", utc_now()
                    active.error = "Ownership expired; subsequent ingestion must reconcile"
            if candidate.attempts >= self.max_attempts:
                candidate.status, candidate.error = "FAILED", "Ingestion retry limit reached"
                candidate.completed_at = utc_now()
                candidate.lease_until = None
                s.get(RepositoryModel, candidate.repository_id).status = "FAILED"
                state.active_run_id = None
                if state.state != "READY":
                    state.state = "REPAIR_REQUIRED"
                return None
            repo = s.get(RepositoryModel, candidate.repository_id)
            if (candidate.trigger == "adr" and candidate.attempts == 0
                    and (state.state != "READY" or repo.indexed_commit_sha != candidate.target_commit_sha)):
                candidate.status, candidate.completed_at = "SUPERSEDED", utc_now()
                candidate.error = "Baseline changed before ADR ownership"
                return None
            candidate.base_commit_sha = repo.indexed_commit_sha
            candidate.force_full = int(bool(candidate.force_full) or state.state != "READY"
                                       or candidate.attempts > 0)
            candidate.is_incremental = int(bool(repo.indexed_commit_sha) and not candidate.force_full)
            state.epoch += 1
            state.state, state.active_run_id = "BUILDING", run_id
            candidate.owner = uuid.uuid4().hex
            candidate.token = state.epoch
            candidate.status = "INDEXING"
            candidate.lease_until = now + self.lease_seconds
            candidate.attempts += 1
            candidate.completed_at, candidate.error = None, None
            repo.status = "INDEXING"
            s.flush()
            return candidate

    def _owned(self, s, run_id, owner, *, require_live=True):
        run = s.get(IngestionRunModel, run_id)
        if run is None:
            raise LostIngestionLease("Unknown ingestion")
        state = self._lock(s, run.repository_id)
        s.refresh(run)
        if (state.active_run_id != run_id or state.epoch != run.token
                or run.owner != owner or run.status != "INDEXING"
                or (require_live and run.lease_until <= self._now(s))):
            raise LostIngestionLease("Ingestion ownership changed")
        return run, state

    def heartbeat(self, run_id, owner):
        with self.sessions.begin() as s:
            run, _ = self._owned(s, run_id, owner)
            run.lease_until = self._now(s) + self.lease_seconds

    def finish(self, run_id, owner, target):
        with self.sessions.begin() as s:
            run, state = self._owned(s, run_id, owner)
            if target != run.target_commit_sha:
                raise ValueError("Published target differs from accepted target")
            repo = s.get(RepositoryModel, run.repository_id)
            if repo.installation_id != run.installation_id or repo.tracked_branch != run.branch:
                raise LostIngestionLease("Repository binding changed")
            run.status, run.completed_at, run.error = "COMPLETED", utc_now(), None
            run.lease_until = None
            repo.status, repo.indexed_commit_sha = "COMPLETED", target
            state.state, state.active_run_id = "READY", None
            state.published_run_id, state.published_token = run.id, run.token

    def recover_sealed(self, run_id, seal):
        """A COMPLETE graph cannot mutate under this token; SQL publication is retryable."""
        if not seal or seal.get("state") != "COMPLETE":
            return False
        with self.sessions.begin() as s:
            run = s.get(IngestionRunModel, run_id)
            if run is None:
                return False
            state = self._lock(s, run.repository_id)
            s.refresh(run)
            repo = s.get(RepositoryModel, run.repository_id)
            if (state.active_run_id != run_id or state.epoch != run.token
                    or run.status != "INDEXING" or seal.get("run_id") != run_id
                    or seal.get("token") != run.token or seal.get("target") != run.target_commit_sha
                    or repo.tracked_branch != run.branch or repo.installation_id != run.installation_id):
                return False
            run.status, run.completed_at, run.lease_until = "COMPLETED", utc_now(), None
            run.error = None
            repo.status, repo.indexed_commit_sha = "COMPLETED", run.target_commit_sha
            state.state, state.active_run_id = "READY", None
            state.published_run_id, state.published_token = run_id, run.token
            return True

    def fail(self, run_id, owner, error_type="ProcessingFailure", *, superseded=False):
        with self.sessions.begin() as s:
            run, state = self._owned(s, run_id, owner, require_live=False)
            run.error = error_type[:100]  # Never persist raw provider/Git errors.
            run.status = "SUPERSEDED" if superseded else (
                "RETRY" if run.attempts < self.max_attempts else "FAILED")
            run.completed_at, run.lease_until = utc_now(), None
            run.force_full = 1
            run.next_attempt_at = int(self.clock()) + min(300, 10 * 2 ** run.attempts)
            state.active_run_id, state.state = None, "REPAIR_REQUIRED"
            s.get(RepositoryModel, run.repository_id).status = "FAILED"
