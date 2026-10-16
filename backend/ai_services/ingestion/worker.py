"""Long-running, restartable baseline ingestion; SQL is the durable queue."""
import asyncio
import tempfile
from pathlib import Path
from .graph_control import FencedDriver
from .persistence.ingestion_jobs import LostIngestionLease
from .service import RepositoryIngestionService
from .rkg.neo4j_writer import Neo4jRepositoryWriter


class _Metadata:
    """Existing pipeline writes belong to the claimed run, not a second history."""
    def __init__(self, store, run):
        self.store, self.run = store, run

    def __getattr__(self, name):
        return getattr(self.store, name)

    def save_repository(self, **_):
        return self.store.get_repository(self.run.repository_id)

    def record_ingestion_start(self, **kwargs):
        if kwargs["target_commit_sha"] != self.run.target_commit_sha:
            raise ValueError("Pipeline target changed")
        return self.run

    def record_ingestion_success(self, **kwargs):
        if kwargs["commit_sha"] != self.run.target_commit_sha:
            raise ValueError("Pipeline completion target changed")

    def record_ingestion_failure(self, *_):
        pass  # Worker records a sanitized, fenced failure.


class _PinnedSource:
    def __init__(self, source, snapshot):
        self.source, self.snapshot = source, snapshot

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def prepare_snapshot(self, *_args, **_kwargs):
        return self.snapshot

    update_snapshot = prepare_snapshot


class IngestionWorker:
    def __init__(self, jobs, application_store, source, github, control, *, llm, embedder):
        self.jobs, self.store, self.source = jobs, application_store, source
        self.github, self.control = github, control
        self.llm, self.embedder = llm, embedder

    async def _heartbeat(self, run):
        while True:
            await asyncio.sleep(20)
            await asyncio.to_thread(self.jobs.heartbeat, run.id, run.owner)
            current = self.jobs.get(run.id)
            await asyncio.to_thread(self.control.renew, run, current.lease_until)

    async def _fresh(self, run, repo, credential):
        ref = self.source.resolve_ref(repo.full_name, branch=run.branch)
        return await self.source.resolve_target(ref, credential)

    async def run(self, run_id):
        from shared.utils.processing import require_background_processing
        require_background_processing()
        existing = self.jobs.get(run_id)
        if existing is None:
            return
        seal = await asyncio.to_thread(self.control.snapshot, existing.repository_id)
        if self.jobs.recover_sealed(run_id, seal):
            return
        run = self.jobs.claim(run_id)
        if run is None:
            return
        heartbeat = None
        try:
            await asyncio.to_thread(self.control.arm, run)
            heartbeat = asyncio.create_task(self._heartbeat(run))
            execution = asyncio.create_task(self._execute(run))
            done, _ = await asyncio.wait([heartbeat, execution], return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                await heartbeat
                raise LostIngestionLease("Heartbeat stopped")
            await execution
        except asyncio.CancelledError:
            if "execution" in locals():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            raise  # Lease expiry recovers hard/soft interruption.
        except LostIngestionLease:
            pass  # Never overwrite another owner's state.
        except Exception as exc:
            # If graph sealed but SQL commit failed, preserve it for recovery.
            seal = await asyncio.to_thread(self.control.snapshot, run.repository_id)
            if not (seal and seal["state"] == "COMPLETE" and seal["token"] == run.token):
                try:
                    self.jobs.fail(run.id, run.owner, type(exc).__name__)
                except LostIngestionLease:
                    pass
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _execute(self, run):
        repo = self.store.get_repository(run.repository_id)
        credential = await self.github.indexing_credential(repo.id, run.installation_id)
        current = await self._fresh(run, repo, credential)
        if current != run.target_commit_sha:
            self.jobs.enqueue(repo.id, current, branch=run.branch,
                installation_id=run.installation_id, trigger="branch_refresh", force_full=True)
            self.jobs.fail(run.id, run.owner, "TargetSuperseded", superseded=True)
            return
        ref = self.source.resolve_ref(repo.full_name, branch=run.branch)
        async with self.source.open_workspace(ref, run.target_commit_sha,
                run.base_commit_sha, credential) as (snapshot, base_available):
            driver = FencedDriver(self.control, run)
            with tempfile.TemporaryDirectory(prefix="decisionguard-ingestion-audit-") as audit:
                service = RepositoryIngestionService(
                    _PinnedSource(self.source, snapshot), _Metadata(self.store, run),
                    driver, database=self.control.database, llm=self.llm, embedder=self.embedder,
                    github_app=self.github, state_dir=Path(audit))
                await service.ingest_repository(repo.full_name, repository_id=repo.id,
                    installation_id=run.installation_id, user_id=repo.user_id,
                    branch=run.branch, force_full=bool(run.force_full) or not base_available,
                    trigger="webhook_push", credential=credential)
                writer = Neo4jRepositoryWriter(driver, self.control.database)
                writer.validate(repo.id)
        # Recheck current installation and branch just before sealing/publication.
        credential = await self.github.indexing_credential(repo.id, run.installation_id)
        current = await self._fresh(run, repo, credential)
        if current != run.target_commit_sha:
            self.jobs.enqueue(repo.id, current, branch=run.branch,
                installation_id=run.installation_id, trigger="branch_refresh", force_full=True)
            self.jobs.fail(run.id, run.owner, "TargetSuperseded", superseded=True)
            return
        self.jobs.heartbeat(run.id, run.owner)
        await asyncio.to_thread(self.control.seal, run)
        self.jobs.finish(run.id, run.owner, run.target_commit_sha)
