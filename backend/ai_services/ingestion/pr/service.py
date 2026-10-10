from __future__ import annotations

import asyncio
from collections import Counter
import logging
from pathlib import Path
from typing import Callable

from ai_services.github_app.service import GitHubAppService
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.sources.github import GitHubRepositorySource
from .git_reader import PullRequestGitReader
from .models import PullRequestChangeSet, PullRequestEvent, PullRequestIngestionError, SupersededRevisionError
from .settings import PullRequestSettings

logger = logging.getLogger(__name__)


class PullRequestIngestionService:
    def __init__(self, repository_source: GitHubRepositorySource, metadata_store: SqliteApplicationStore,
                 github_app: GitHubAppService, repository_lock: Callable[[str], asyncio.Lock],
                 settings: PullRequestSettings | None = None, state_dir: Path | str = '.state'):
        self.source, self.store, self.github_app = repository_source, metadata_store, github_app
        self.repository_lock = repository_lock
        self.settings = settings or PullRequestSettings.from_env()
        self.reader = PullRequestGitReader(repository_source, self.settings)
        self.state_dir = Path(state_dir)

    def _tracked(self, event):
        repo = self.store.get_repository(str(event.github_repository_id))
        if not repo or repo.github_repository_id != str(event.github_repository_id):
            raise PullRequestIngestionError('PR repository is not tracked')
        if (repo.installation_id != str(event.installation_id) or event.repository_private
                or event.base_repository_id != event.github_repository_id
                or repo.full_name.lower() != event.repository_full_name.lower()):
            raise PullRequestIngestionError('PR installation/repository identity or public eligibility mismatch')
        if not repo.indexed_commit_sha:
            raise PullRequestIngestionError('Initial accepted repository ingestion must complete first')
        return repo

    def artifact_path(self, event):
        return self.state_dir / 'pull_requests' / str(event.github_repository_id) / str(event.pull_request_number) / (event.revision_key + '.json')

    def _write_artifact(self, event, change_set):
        path = self.artifact_path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(change_set.model_dump_json(indent=2), encoding='utf-8')
        temporary.replace(path)

    async def ingest_pull_request(self, event: PullRequestEvent) -> PullRequestChangeSet | None:
        # Backend use outside the webhook still receives the same typed contract.
        repo = self._tracked(event)
        if not self.store.claim_pr_revision(repo.id, event):
            logger.info('PR revision duplicate skipped repository=%s pr=%s revision=%s delivery=%s',
                        repo.id, event.pull_request_number, event.revision_key, event.delivery_id)
            return None
        logger.info('PR ingestion requested repository=%s github_repository=%s installation=%s pr=%s base=%s head=%s delivery=%s',
            repo.id, event.github_repository_id, event.installation_id, event.pull_request_number,
            event.base_sha, event.head_sha, event.delivery_id)
        try:
            stage = 'installation authorization'
            async with self.repository_lock(repo.full_name):
                # Acquire fresh installation access at execution time, not user OAuth.
                repo = self._tracked(event)
                credential = await self.github_app.indexing_credential(repo.id, str(event.installation_id))
                ref = self.source.resolve_ref(repo.full_name)
                stage = 'clone/object/diff reconstruction'
                root = self.source._repo_clone_path(ref).resolve()
                if not root.is_relative_to(self.source.storage_dir):
                    raise PullRequestIngestionError('Clone path escapes repository storage')
                worker = asyncio.create_task(asyncio.to_thread(self.reader.read_revision,
                    root, event, credential, f'https://github.com/{repo.full_name}.git'))
                try:
                    merge_base, files = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Threads cannot be cancelled. Keep the shared clone lock
                    # until Git exits, so a shutdown cannot overlap checkout/reset.
                    try:
                        await worker
                    except Exception:
                        pass
                    raise
                # Revocation/binding changes during fetch must not publish a result.
                self._tracked(event)
                stage = 'publication authorization'
                await self.github_app.indexing_credential(repo.id, str(event.installation_id))
            change_set = PullRequestChangeSet(**event.model_dump(), repository_id=repo.id,
                                             merge_base_sha=merge_base, files=files)
            if self.settings.write_debug_artifacts:
                stage = 'debug artifact'
                await asyncio.to_thread(self._write_artifact, event, change_set)
            stage = 'revision completion'
            self.store.finish_pr_revision(event.revision_key, 'COMPLETED')
            logger.info('PR change set constructed repository=%s pr=%s revision=%s files=%s statuses=%s delivery=%s',
                repo.id, event.pull_request_number, event.revision_key, len(files),
                dict(Counter(file.status for file in files)), event.delivery_id)
            # Future analysis engine consumes this typed object here; no LLM/RKG call.
            return change_set
        except asyncio.CancelledError:
            self.store.finish_pr_revision(event.revision_key, 'FAILED', 'Cancelled', 'PR worker cancelled; retry delivery')
            raise
        except Exception as exc:
            status = 'SUPERSEDED' if isinstance(exc, SupersededRevisionError) else 'FAILED'
            # Keep source/credentials out of DB/logs even if an upstream exception is unsafe.
            self.store.finish_pr_revision(event.revision_key, status, type(exc).__name__,
                f'{stage}: {str(exc) if isinstance(exc, PullRequestIngestionError) else "operation failed; retry delivery"}'[:500])
            logger.error('PR ingestion %s repository=%s pr=%s base=%s head=%s delivery=%s stage=%s error_type=%s',
                status.lower(), repo.id, event.pull_request_number, event.base_sha, event.head_sha,
                event.delivery_id, stage, type(exc).__name__)
            raise
