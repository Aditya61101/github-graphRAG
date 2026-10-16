"""Small adapter keeping ADR and code writers in the same repository fence."""
import asyncio
import copy
import tempfile
from ai_services.ingestion.graph_control import FencedDriver
from ai_services.ingestion.readiness import GraphUnavailable
from ai_services.ingestion.service import RepositoryIngestionService
from ai_services.ingestion.persistence.ingestion_jobs import LostIngestionLease


class GuardedADRProcessor:
    def __init__(self, processor, store, jobs, control, barrier, llm, embedder):
        self.processor, self.store, self.jobs = processor, store, jobs
        self.control, self.barrier = control, barrier
        self.llm, self.embedder = llm, embedder

    async def process_adr(self, doc):
        repo = self.store.get_repository(doc.repository_id)
        self.barrier.snapshot(repo.id)
        run_id = self.jobs.enqueue(repo.id, repo.indexed_commit_sha,
            branch=repo.tracked_branch, installation_id=repo.installation_id, trigger="adr")
        run = self.jobs.claim(run_id)
        if run is None:
            raise GraphUnavailable()
        heartbeat = None
        work = None
        try:
            await asyncio.to_thread(self.control.arm, run)
            async def beat():
                while True:
                    await asyncio.sleep(20)
                    self.jobs.heartbeat(run.id, run.owner)
                    await asyncio.to_thread(self.control.renew, run, self.jobs.get(run.id).lease_until)
            async def execute():
                processor = copy.copy(self.processor)
                processor.writer = copy.copy(self.processor.writer)
                processor.resolver = copy.copy(self.processor.resolver)
                driver = FencedDriver(self.control, run)
                processor.writer.driver = processor.resolver.driver = driver
                await processor.process_adr(doc)
                with tempfile.TemporaryDirectory(prefix="decisionguard-adr-audit-") as scratch:
                    service = RepositoryIngestionService(None, self.store, driver,
                        database=self.control.database, state_dir=scratch,
                        llm=self.llm, embedder=self.embedder)
                    await service._run_community_pipeline(repo.id)
                self.jobs.heartbeat(run.id, run.owner)
                await asyncio.to_thread(self.control.seal, run)
                self.jobs.finish(run.id, run.owner, run.target_commit_sha)
            heartbeat, work = asyncio.create_task(beat()), asyncio.create_task(execute())
            done, _ = await asyncio.wait([heartbeat, work], return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                await heartbeat
                raise LostIngestionLease("ADR heartbeat stopped")
            await work
        except BaseException:
            if work:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            try:
                seal = self.control.snapshot(repo.id)
                if not (seal and seal["token"] == run.token and seal["state"] == "COMPLETE"):
                    self.jobs.fail(run.id, run.owner, "ADRProcessingFailure")
            except LostIngestionLease:
                pass
            raise
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
