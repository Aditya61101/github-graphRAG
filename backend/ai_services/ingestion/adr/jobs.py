"""ADR intents reuse repository ingestion ownership, without target coalescing."""
import uuid
from sqlalchemy import select
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
from ai_services.ingestion.persistence.models import (
    ADRModel, ADRIngestionRun, IngestionRunModel, RepositoryModel,
    utc_now,
)


class ADRJobs(IngestionJobs):
    def mapping(self, run_id):
        with self.sessions() as s:
            return s.scalar(select(ADRIngestionRun).where(ADRIngestionRun.run_id == run_id))

    def for_adr(self, adr_id):
        with self.sessions() as s:
            mapping = s.get(ADRIngestionRun, adr_id)
            return s.get(IngestionRunModel, mapping.run_id) if mapping else None

    def set_title(self, run_id, owner, title):
        with self.sessions.begin() as s:
            run, _ = self._owned(s, run_id, owner)
            mapping = s.scalar(select(ADRIngestionRun).where(ADRIngestionRun.run_id == run.id))
            adr = s.get(ADRModel, mapping.adr_id)
            adr.title, adr.updated_at = title, utc_now()

    def enqueue_adr(self, adr_id, *, retry=False):
        with self.sessions.begin() as s:
            adr = s.get(ADRModel, adr_id)
            if adr is None:
                raise ValueError("Unknown ADR")
            self._lock(s, adr.repository_id)
            s.refresh(adr)
            repo = s.get(RepositoryModel, adr.repository_id)
            if not repo.indexed_commit_sha or not repo.installation_id:
                raise ValueError("Repository baseline ingestion must complete first")
            mapping = s.get(ADRIngestionRun, adr_id)
            now = self._now(s)
            if mapping:
                run = s.get(IngestionRunModel, mapping.run_id)
                if run.status in {"QUEUED", "RETRY", "INDEXING", "COMPLETED"}:
                    return run.id
                if not retry:
                    return run.id
                # The old owner must be fenced out by claim before graph writes.
                run.status, run.attempts, run.next_attempt_at = "QUEUED", 0, now
                run.error, run.completed_at = None, None
            else:
                if adr.status == "COMPLETED":
                    return None  # Existing pre-async document; never re-extract automatically.
                run = IngestionRunModel(id="run_" + uuid.uuid4().hex,
                    repository_id=repo.id, target_commit_sha=repo.indexed_commit_sha,
                    branch=repo.tracked_branch, installation_id=repo.installation_id,
                    trigger="adr", status="QUEUED", next_attempt_at=now)
                s.add(run)
                s.flush()
                s.add(ADRIngestionRun(adr_id=adr_id, run_id=run.id))
            adr.status, adr.error, adr.updated_at = "PENDING", None, utc_now()
            return run.id

    def claim(self, run_id):
        with self.sessions.begin() as s:
            candidate = s.get(IngestionRunModel, run_id)
            if not candidate or candidate.trigger != "adr":
                return None
            state = self._lock(s, candidate.repository_id)
            s.refresh(candidate)
            mapping = s.scalar(select(ADRIngestionRun).where(ADRIngestionRun.run_id == run_id))
            if mapping is None:
                raise ValueError("Legacy ADR run has no source mapping; manual reconciliation required")
            now = self._now(s)
            if candidate.status not in {"QUEUED", "RETRY", "INDEXING"} or candidate.next_attempt_at > now:
                return None
            if state.active_run_id:
                active = s.get(IngestionRunModel, state.active_run_id)
                if active and active.lease_until and active.lease_until > now:
                    return None
                if active and active.id != run_id and active.status == "INDEXING":
                    return None  # Let its worker recover the seal/expired ownership first.
            if candidate.attempts >= self.max_attempts:
                candidate.status, candidate.lease_until = "FAILED", None
                candidate.completed_at, candidate.error = utc_now(), "ADR retry limit reached"
                adr = s.get(ADRModel, mapping.adr_id)
                adr.status, adr.error, adr.updated_at = "FAILED", "ADR processing failed; retry available", utc_now()
                state.active_run_id, state.state = None, "REPAIR_REQUIRED"
                s.get(RepositoryModel, candidate.repository_id).status = "FAILED"
                return None
            repo = s.get(RepositoryModel, candidate.repository_id)
            if not repo.indexed_commit_sha:
                return None
            # An ADR may repair an interrupted ADR graph, never a dirty code graph.
            if state.state != "READY":
                previous = s.scalar(select(IngestionRunModel).where(
                    IngestionRunModel.repository_id == repo.id,
                    IngestionRunModel.token == state.epoch))
                if not previous or previous.trigger != "adr":
                    return None
            candidate.target_commit_sha = repo.indexed_commit_sha
            candidate.base_commit_sha = repo.indexed_commit_sha
            candidate.branch, candidate.installation_id = repo.tracked_branch, repo.installation_id
            state.epoch += 1
            state.state, state.active_run_id = "BUILDING", run_id
            candidate.owner, candidate.token = uuid.uuid4().hex, state.epoch
            candidate.status, candidate.lease_until = "INDEXING", now + self.lease_seconds
            candidate.attempts += 1
            candidate.error, candidate.completed_at = None, None
            repo.status = "INDEXING"
            adr = s.get(ADRModel, mapping.adr_id)
            adr.status, adr.error, adr.updated_at = "PROCESSING", None, utc_now()
            s.flush()
            return candidate

    def fail(self, run_id, owner, error_type="ADRProcessingFailure", **kwargs):
        # Repository failure and per-document failure belong to one transaction.
        with self.sessions.begin() as s:
            run, state = self._owned(s, run_id, owner, require_live=False)
            run.status = "RETRY" if run.attempts < self.max_attempts else "FAILED"
            run.error, run.lease_until, run.completed_at = "ADRProcessingFailure", None, utc_now()
            run.next_attempt_at = self._now(s) + min(300, 10 * 2 ** run.attempts)
            state.active_run_id, state.state = None, "REPAIR_REQUIRED"
            s.get(RepositoryModel, run.repository_id).status = "FAILED"
            mapping = s.scalar(select(ADRIngestionRun).where(ADRIngestionRun.run_id == run.id))
            adr = s.get(ADRModel, mapping.adr_id)
            adr.status = "PENDING" if run.status == "RETRY" else "FAILED"
            adr.error = "ADR processing failed; retry scheduled" if run.status == "RETRY" else "ADR processing failed; retry available"
            adr.updated_at = utc_now()
