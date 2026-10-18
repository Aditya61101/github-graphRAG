"""Durable ADR execution; all mutations share the existing repository fence."""
import asyncio
import copy
import hashlib
import logging
import tempfile
from .parser import parse_adr_content
from ai_services.ingestion.graph_control import FencedDriver
from ai_services.ingestion.persistence.ingestion_jobs import LostIngestionLease
from ai_services.ingestion.service import RepositoryIngestionService

logger = logging.getLogger(__name__)


class ADRWorker:
    def __init__(self, jobs, store, originals, processor, control, *, llm, embedder,
                 github, max_file_size=10 * 1024 * 1024):
        self.jobs, self.store, self.originals = jobs, store, originals
        self.processor, self.control = processor, control
        self.llm, self.embedder, self.github = llm, embedder, github
        self.max_file_size = max_file_size

    async def _heartbeat(self, run):
        while True:
            await asyncio.sleep(20)
            await asyncio.to_thread(self.jobs.heartbeat, run.id, run.owner)
            await asyncio.to_thread(self.control.renew, run, self.jobs.get(run.id).lease_until)

    async def run(self, run_id):
        from shared.utils.processing import require_background_processing
        require_background_processing()
        mapping = self.jobs.mapping(run_id)
        existing = self.jobs.get(run_id)
        if not mapping or not existing or existing.trigger not in {'adr', 'adr_delete'}:
            raise ValueError('Unknown ADR execution mapping')
        seal = await asyncio.to_thread(self.control.snapshot, existing.repository_id)
        if self.jobs.recover_sealed(run_id, seal):
            return
        from ai_services.ingestion.persistence.models import RepositoryIngestionState
        with self.jobs.sessions() as s:
            state = s.get(RepositoryIngestionState, existing.repository_id)
            ready = state is not None and state.state == 'READY'
        if ready:
            from ai_services.ingestion.readiness import GraphReadBarrier
            GraphReadBarrier(self.jobs.sessions, self.control).snapshot(
                existing.repository_id, allow_pending_deletion=True)
        run = self.jobs.claim(run_id)
        if run is None:
            return  # A duplicate launch/another repository owner is harmless.
        heartbeat = work = None
        try:
            await asyncio.to_thread(self.control.arm, run)
            heartbeat = asyncio.create_task(self._heartbeat(run))
            work = asyncio.create_task(self._execute(run, mapping.adr_id))
            done, _ = await asyncio.wait([heartbeat, work], return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                await heartbeat
                raise LostIngestionLease('ADR heartbeat stopped')
            await work
        except BaseException as exc:
            if work:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            # Ambiguous seal/SQL completion is reconciled on the next launch;
            # never turn a sealed run into a new mutation attempt.
            try:
                seal = await asyncio.to_thread(self.control.snapshot, run.repository_id)
                if not (seal and seal['state'] == 'COMPLETE' and seal['token'] == run.token):
                    self.jobs.fail(run.id, run.owner)
            except LostIngestionLease:
                pass
            logger.warning('ADR execution deferred run=%s adr=%s type=%s', run.id, mapping.adr_id, type(exc).__name__)
            raise
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _execute(self, run, adr_id):
        record = self.store.get_adr(adr_id, include_deleted=True)
        if record.status == 'DELETED':
            # Also handles an expired old ingestion after deletion: it must
            # drain its repository fence without ever resurrecting the ADR.
            from .neo4j_writer import ADRNeo4jWriter
            driver = FencedDriver(self.control, run)
            await asyncio.to_thread(ADRNeo4jWriter(driver, self.control.database).delete_adr,
                                    adr_id, run.repository_id)
            with tempfile.TemporaryDirectory(prefix='decisionguard-adr-delete-') as scratch:
                service = RepositoryIngestionService(None, self.store, driver,
                    database=self.control.database, state_dir=scratch,
                    llm=self.llm, embedder=self.embedder)
                await service._run_community_pipeline(run.repository_id, reuse_unchanged=True)
            await asyncio.to_thread(self.originals.delete, record.file_path,
                                    record.repository_id, record.id)
            self.jobs.heartbeat(run.id, run.owner)
            self.control.seal(run)
            self.jobs.finish(run.id, run.owner, run.target_commit_sha)
            return
        # Revalidate access using the persisted user/installation before reading
        # and again before publication. Never infer ownership from the upload.
        await self.github.indexing_credential(run.repository_id, run.installation_id)
        data = await asyncio.to_thread(self.originals.read, record.file_path,
                                      record.repository_id, record.id, self.max_file_size)
        if len(data) > self.max_file_size or hashlib.sha256(data).hexdigest() != record.content_hash:
            raise ValueError('ADR original integrity validation failed')
        doc = parse_adr_content(file_bytes=data, file_extension=record.file_extension,
            source_name=record.source_name, adr_id=record.id, repository_id=record.repository_id,
            file_path=record.file_path, content_hash=record.content_hash,
            source_type=record.source_type, source_id=record.source_id,
            source_url=record.source_url, source_version=record.source_version,
            mime_type=record.mime_type)
        self.jobs.set_title(run.id, run.owner, doc.title)
        driver = FencedDriver(self.control, run)
        processor = copy.copy(self.processor)
        processor.writer = copy.copy(self.processor.writer)
        processor.resolver = copy.copy(self.processor.resolver)
        processor.writer.driver = processor.resolver.driver = driver
        # A failed predecessor may have committed its document graph before a
        # mandatory later stage failed. Remove only unpublished document-owned
        # knowledge before publishing another ADR; shared entities survive.
        for other in self.store.list_adrs(run.repository_id):
            if other.id == adr_id or other.status == 'COMPLETED':
                continue
            result = driver.execute_query(
                "MATCH (a:ADR {id:$adr, repository:$repo, status:'GRAPH_PERSISTED'}) RETURN a.id",
                adr=other.id, repo=run.repository_id)
            rows = result.records if hasattr(result, 'records') else result[0]
            if rows:
                processor.writer.delete_stale_adr_knowledge(other.id, run.repository_id)
                driver.execute_query("MATCH (a:ADR {id:$adr, repository:$repo}) SET a.status='FAILED'",
                                     adr=other.id, repo=run.repository_id)
        await processor.process_adr(doc)
        with tempfile.TemporaryDirectory(prefix='decisionguard-adr-audit-') as scratch:
            service = RepositoryIngestionService(None, self.store, driver,
                database=self.control.database, state_dir=scratch, llm=self.llm, embedder=self.embedder)
            await service._run_community_pipeline(run.repository_id)
        await self.github.indexing_credential(run.repository_id, run.installation_id)
        self.jobs.heartbeat(run.id, run.owner)
        self.control.seal_adr(run, adr_id)
        # Run, repository readiness, and ADR completion commit atomically in SQL.
        self.jobs.finish(run.id, run.owner, run.target_commit_sha)
        logger.info('ADR published run=%s adr=%s', run.id, adr_id)
