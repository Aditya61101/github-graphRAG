from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neo4j import Driver

from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.ingestion.community import CommunityConfig, build_community_pipeline
from ai_services.ingestion.discovery.git_tree import get_repository_files
from ai_services.ingestion.discovery.planner import MODEL as PLANNER_MODEL, create_ingestion_plan
from ai_services.ingestion.discovery.plan_validation import (
    PlannerCoverageError, resolve_planned_file, validate_ingestion_plan,
)
from ai_services.ingestion.discovery.git_tree import GitFile
from ai_services.ingestion.discovery.repo_manifest import build_repository_manifest
from ai_services.ingestion.run_audit import (
    IngestionRunAudit, IngestionStageCounts, measure_persisted_counts,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.rkg.builders.repo_ingestion_pipeline import (
    build_repository_ingestion_pipeline,
)
from ai_services.ingestion.rkg.incremental import ChangeKind
from ai_services.ingestion.sources.classifier import classify_file_for_ingestion
from ai_services.ingestion.sources.credentials import GitHubCredential, sanitize_sensitive_text
from ai_services.ingestion.sources.interface import (
    RepositoryMetadata,
    RepositoryRef,
    RepositorySnapshot,
    RepositorySource,
)
from ai_services.models.ingestion_plan import IngestionPlan
from shared.utils.llm_utils import AzureOpenAILLM

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    repository: str
    repository_id: str | None
    status: str  # "completed", "already_indexed", "failed", "ignored"
    indexed_commit_sha: str | None
    is_incremental: bool
    added_or_modified_count: int = 0
    deleted_count: int = 0
    message: str = ""
    run_id: str | None = None
    stage_counts: IngestionStageCounts | None = None


class RepositoryIngestionService:
    """Coordinates repository sources, SQLite relational persistence, and repository-scoped RKG ingestion."""

    def __init__(
        self,
        repository_source: RepositorySource,
        metadata_store: SqliteApplicationStore,
        neo4j_driver: Driver,
        database: str | None = None,
        embedder: AzureOpenAIEmbedder | None = None,
        llm: AzureOpenAILLM | None = None,
        groq_client: Any | None = None,
        state_dir: Path | str = ".state",
    ) -> None:
        self.repository_source = repository_source
        self.metadata_store = metadata_store
        self.neo4j_driver = neo4j_driver
        self.database = database
        self.embedder = embedder
        self.llm = llm
        self.groq_client = groq_client
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, repo_key: str) -> asyncio.Lock:
        if repo_key not in self._locks:
            self._locks[repo_key] = asyncio.Lock()
        return self._locks[repo_key]

    async def ingest_repository(
        self,
        repo_identifier: str,
        branch: str | None = None,
        force_full: bool = False,
        credential: GitHubCredential | None = None,
        user_id: str | None = None,
        connection_id: str | None = None,
        trigger: str = "manual",
    ) -> IngestionResult:
        """Trigger initial or incremental repository ingestion with user credential."""
        from ai_services.ingestion.sources.interface import RepositoryAuthenticationError

        ref = self.repository_source.resolve_ref(repo_identifier, branch=branch)

        # Resolve credential:
        # User-triggered flow: application User.id -> user's GitHubConnection -> access token
        # Webhook-triggered flow: connection_id -> Repository's GitHubConnection -> access token
        # NEVER select a credential merely from a repository name!
        cred = credential
        if not cred and user_id:
            cred = self.metadata_store.get_credential_for_user(user_id)
        elif not cred and connection_id:
            cred = self.metadata_store.get_credential_by_connection_id(connection_id)

        if not cred:
            raise RepositoryAuthenticationError(
                f"No valid GitHub credential found for repository '{ref.full_name}'. "
                "Authentication required."
            )

        lock = self._get_lock(ref.full_name)
        async with lock:
            metadata = await self.repository_source.get_metadata(ref, credential=cred)

            # Public repositories only
            if metadata.visibility == "private":
                from ai_services.ingestion.sources.interface import PrivateRepositoryUnsupportedError
                raise PrivateRepositoryUnsupportedError(
                    f"Repository '{ref.full_name}' is private. "
                    "The current DecisionGuard prototype supports public GitHub repositories only."
                )

            repo_record = self.metadata_store.save_repository(
                metadata=metadata,
                user_id=user_id,
                connection_id=connection_id,
            )
            indexed_sha = repo_record.indexed_commit_sha

            if not indexed_sha or force_full:
                return await self._run_full_ingestion(
                    ref=ref,
                    metadata=metadata,
                    repo_id=repo_record.id,
                    credential=cred,
                    trigger=trigger,
                )
            else:
                return await self._run_incremental_ingestion(
                    ref=ref,
                    metadata=metadata,
                    repo_id=repo_record.id,
                    indexed_sha=indexed_sha,
                    credential=cred,
                    trigger=trigger,
                )

    async def _run_full_ingestion(
        self,
        ref: RepositoryRef,
        metadata: RepositoryMetadata,
        repo_id: str,
        credential: GitHubCredential | None = None,
        trigger: str = "initial",
    ) -> IngestionResult:
        logger.info(
            f"Starting FULL ingestion for {ref.full_name} (id={repo_id}) on branch {ref.branch or metadata.default_branch}"
        )
        snapshot = await self.repository_source.prepare_snapshot(ref, credential=credential)
        run = self.metadata_store.record_ingestion_start(
            repository_id=repo_id,
            target_commit_sha=snapshot.commit_sha,
            base_commit_sha=None,
            trigger=trigger,
            is_incremental=False,
        )

        audit = None
        try:
            audit = IngestionRunAudit(
                self.state_dir, repo_id, run.id, mode="full",
                commit=snapshot.commit_sha, branch=snapshot.branch, status="running",
            )
            # 1. Discovery and Manifest
            files = get_repository_files(snapshot.root)
            manifest = build_repository_manifest(
                repo_name=ref.name,
                commit=snapshot.commit_sha,
                files=files,
            )
            audit.write("manifest.json", manifest.to_dict())
            audit.counts.discovered = len(files)
            audit.save_counts()

            # 2. Planning (use Groq planner if client available, otherwise fallback to classifier)
            architectural_objective = (
                "Build an architectural knowledge graph for DecisionGuard. "
                "Prioritize production components, services, APIs, databases, "
                "events, contracts, and configuration."
            )
            planner_metadata = {
                "source": "groq" if self.groq_client else "classifier",
                "objective": architectural_objective, "scope": "complete_manifest",
                "requested_model": PLANNER_MODEL if self.groq_client else None,
            }
            def record_response(content, metadata):
                audit.record_planner_response(content, metadata)
                planner_metadata.update(metadata)

            if self.groq_client:
                try:
                    plan = await asyncio.to_thread(
                        create_ingestion_plan,
                        client=self.groq_client,
                        architectural_objective=architectural_objective,
                        manifest=manifest.to_dict(),
                        on_response=record_response,
                    )
                    validate_ingestion_plan(plan, manifest.to_dict())
                except Exception as exc:
                    error = sanitize_sensitive_text(str(exc))
                    if isinstance(exc, PlannerCoverageError):
                        logger.warning(
                            "Groq planner coverage invalid counts=%s; falling back to deterministic classifier.",
                            {key: len(value) for key, value in exc.details.items()},
                        )
                    else:
                        logger.warning("Groq planner failed (%s); falling back to deterministic classifier.", error)
                    planner_metadata.update(source="classifier_fallback", fallback_reason=error)
                    if isinstance(exc, PlannerCoverageError):
                        planner_metadata["coverage_errors"] = exc.details
                    plan = IngestionPlan(files=[classify_file_for_ingestion(f.path) for f in files])
            else:
                plan = IngestionPlan(files=[classify_file_for_ingestion(f.path) for f in files])
            validate_ingestion_plan(plan, manifest.to_dict())
            audit.record_plan(manifest, plan, planner_metadata)

            # 3. RKG Ingestion Pipeline scoped to repo_id
            candidates_path = self.state_dir / f"candidates_{ref.owner}_{ref.name}.jsonl"
            pipeline = build_repository_ingestion_pipeline(
                repository_root=snapshot.root,
                repository_id=repo_id,
                repository_name=ref.name,
                commit=snapshot.commit_sha,
                neo4j_driver=self.neo4j_driver,
                candidates_path=candidates_path,
                full_name=metadata.full_name,
                owner=metadata.owner,
                progress_callback=audit.record_result,
            )
            result = await pipeline.ingest(plan.files)
            audit.record_result(result)
            audit.counts.persisted = await asyncio.to_thread(
                measure_persisted_counts, self.neo4j_driver, self.database, repo_id, result,
            )
            audit.save_counts()

            # 4. Community Layer scoped to repo_id
            community_result = await self._run_community_pipeline(repository_id=repo_id)
            audit.counts.communities = community_result.community_count if community_result else None
            audit.finish("completed")
            logger.info("RKG stage counts repository=%s run=%s counts=%s", repo_id, run.id, audit.counts.to_dict())

            # 5. Record Success in SQLite
            self.metadata_store.record_ingestion_success(
                repository_id=repo_id,
                run_id=run.id,
                commit_sha=snapshot.commit_sha,
            )
            logger.info(f"FULL ingestion completed for {ref.full_name} at commit {snapshot.commit_sha}")

            return IngestionResult(
                repository=ref.full_name,
                repository_id=repo_id,
                status="completed",
                indexed_commit_sha=snapshot.commit_sha,
                is_incremental=False,
                added_or_modified_count=audit.counts.chunked,
                run_id=run.id,
                stage_counts=audit.counts,
                message=(
                    f"Full ingestion produced {audit.counts.chunks} chunks from "
                    f"{audit.counts.chunked}/{audit.counts.included} included files "
                    f"({audit.counts.discovered} discovered, {audit.counts.excluded} excluded); "
                    f"persisted {audit.counts.persisted.entities} entities and "
                    f"{audit.counts.persisted.relationships} relationships at commit {snapshot.commit_sha}"
                ),
            )

        except Exception as exc:
            if audit:
                audit.finish("failed", error=sanitize_sensitive_text(str(exc)))
            logger.exception(f"FULL ingestion failed for {ref.full_name}: {exc}")
            self.metadata_store.record_ingestion_failure(repo_id, run.id, str(exc))
            raise

    async def _run_incremental_ingestion(
        self,
        ref: RepositoryRef,
        metadata: RepositoryMetadata,
        repo_id: str,
        indexed_sha: str,
        credential: GitHubCredential | None = None,
        trigger: str = "incremental",
    ) -> IngestionResult:
        logger.info(f"Starting INCREMENTAL ingestion for {ref.full_name} (id={repo_id}) from {indexed_sha}")
        snapshot = await self.repository_source.update_snapshot(ref, credential=credential)

        if snapshot.commit_sha == indexed_sha:
            return IngestionResult(
                repository=ref.full_name,
                repository_id=repo_id,
                status="already_indexed",
                indexed_commit_sha=indexed_sha,
                is_incremental=True,
                message=f"Repository {ref.full_name} is already at indexed commit {indexed_sha}",
            )

        run = self.metadata_store.record_ingestion_start(
            repository_id=repo_id,
            target_commit_sha=snapshot.commit_sha,
            base_commit_sha=indexed_sha,
            trigger=trigger,
            is_incremental=True,
        )

        audit = None
        try:
            audit = IngestionRunAudit(
                self.state_dir, repo_id, run.id, mode="incremental", status="running",
                commit=snapshot.commit_sha, branch=snapshot.branch, base_commit=indexed_sha,
            )
            files = get_repository_files(snapshot.root)
            manifest = build_repository_manifest(ref.name, snapshot.commit_sha, files)
            audit.write("manifest.json", manifest.to_dict())
            audit.counts.discovered = len(files)
            audit.save_counts()
            # 1. Compute git diff between previous indexed commit and new commit
            changes = self.repository_source.compute_diff(
                snapshot=snapshot,
                base_commit=indexed_sha,
                target_commit=snapshot.commit_sha,
            )

            deleted_paths = [c.path for c in changes if c.kind == ChangeKind.DELETED]
            added_or_modified_paths = [
                c.path for c in changes if c.kind in {ChangeKind.ADDED, ChangeKind.MODIFIED}
            ]

            logger.info(
                f"Diff between {indexed_sha[:8]}..{snapshot.commit_sha[:8]}: "
                f"{len(added_or_modified_paths)} added/modified, {len(deleted_paths)} deleted."
            )

            # Incremental planning intentionally covers the changed-file manifest,
            # not unchanged snapshot files. Preserve the complete manifest as well.
            changed_files = []
            for path in added_or_modified_paths:
                resolved = resolve_planned_file(snapshot.root, path)
                changed_files.append(GitFile(path, "100644", "blob", "", resolved.stat().st_size))
            planning_manifest = build_repository_manifest(ref.name, snapshot.commit_sha, changed_files)
            plan = IngestionPlan(files=[classify_file_for_ingestion(p) for p in added_or_modified_paths])
            validate_ingestion_plan(plan, planning_manifest.to_dict())
            audit.record_plan(planning_manifest, plan, {
                "source": "classifier", "scope": "changed_files", "deleted_paths": deleted_paths,
            })

            if not deleted_paths and not added_or_modified_paths:
                audit.finish("completed")
                self.metadata_store.record_ingestion_success(
                    repository_id=repo_id,
                    run_id=run.id,
                    commit_sha=snapshot.commit_sha,
                )
                return IngestionResult(
                    repository=ref.full_name,
                    repository_id=repo_id,
                    status="completed",
                    indexed_commit_sha=snapshot.commit_sha,
                    is_incremental=True,
                    run_id=run.id,
                    stage_counts=audit.counts,
                    message="No file changes between commits.",
                )

            # 2. Plan chunking strategies for added and modified files
            file_plans = plan.files

            # 3. Incremental RKG update scoped to repo_id
            candidates_path = self.state_dir / f"candidates_{ref.owner}_{ref.name}.jsonl"
            pipeline = build_repository_ingestion_pipeline(
                repository_root=snapshot.root,
                repository_id=repo_id,
                repository_name=ref.name,
                commit=snapshot.commit_sha,
                neo4j_driver=self.neo4j_driver,
                candidates_path=candidates_path,
                full_name=metadata.full_name,
                owner=metadata.owner,
                progress_callback=audit.record_result,
            )
            result = await pipeline.ingest_incremental(
                added_or_modified_plans=file_plans,
                deleted_paths=deleted_paths,
            )
            audit.record_result(result)
            audit.counts.persisted = await asyncio.to_thread(
                measure_persisted_counts, self.neo4j_driver, self.database, repo_id, result,
            )
            audit.save_counts()

            # 4. Community Layer scoped to repo_id
            community_result = await self._run_community_pipeline(repository_id=repo_id)
            audit.counts.communities = community_result.community_count if community_result else None
            audit.finish("completed")
            logger.info("RKG stage counts repository=%s run=%s counts=%s", repo_id, run.id, audit.counts.to_dict())

            # 5. Record Success in SQLite
            self.metadata_store.record_ingestion_success(
                repository_id=repo_id,
                run_id=run.id,
                commit_sha=snapshot.commit_sha,
            )
            logger.info(
                f"INCREMENTAL ingestion completed for {ref.full_name} at commit {snapshot.commit_sha}"
            )

            return IngestionResult(
                repository=ref.full_name,
                repository_id=repo_id,
                status="completed",
                indexed_commit_sha=snapshot.commit_sha,
                is_incremental=True,
                added_or_modified_count=len(added_or_modified_paths),
                deleted_count=len(deleted_paths),
                run_id=run.id,
                stage_counts=audit.counts,
                message=(
                    f"Incremental ingestion produced {audit.counts.chunks} chunks from "
                    f"{audit.counts.chunked}/{audit.counts.included} included changed files, "
                    f"deleted {len(deleted_paths)} files from {indexed_sha[:8]} to {snapshot.commit_sha[:8]}."
                ),
            )

        except Exception as exc:
            if audit:
                audit.finish("failed", error=sanitize_sensitive_text(str(exc)))
            logger.exception(f"INCREMENTAL ingestion failed for {ref.full_name}: {exc}")
            self.metadata_store.record_ingestion_failure(repo_id, run.id, str(exc))
            raise

    async def _run_community_pipeline(self, repository_id: str):
        """Run community detection and summarization scoped strictly to the specified repository."""
        if not self.llm or not self.embedder:
            logger.info("Skipping community detection: LLM or Embedder not configured.")
            return

        try:
            config = CommunityConfig(
                graph_name="entityGraph",
                repository_id=repository_id,
                algorithm="leiden",
                random_seed=42,
                database=self.database,
            )
            community_pipeline = build_community_pipeline(
                driver=self.neo4j_driver,
                llm=self.llm,
                embedder=self.embedder,
                config=config,
            )
            return await community_pipeline.run(force_refresh=False)
        except Exception as exc:
            logger.warning(f"Community pipeline error for repo {repository_id}: {exc}")

    async def handle_github_push_webhook(self, payload: dict[str, Any]) -> IngestionResult:
        """Process a GitHub push event on a tracked branch using stored user credential."""
        repo_data = payload.get("repository", {})
        full_name = repo_data.get("full_name")
        github_repo_id = str(repo_data.get("id")) if repo_data.get("id") else None

        if not full_name:
            return IngestionResult(
                repository="unknown",
                repository_id=None,
                status="ignored",
                indexed_commit_sha=None,
                is_incremental=False,
                message="Missing repository.full_name in push payload.",
            )

        # Check if repository is tracked in SQLite store
        tracked_repo = None
        if github_repo_id:
            tracked_repo = self.metadata_store.get_repository(github_repo_id)
        if not tracked_repo:
            tracked_repo = self.metadata_store.get_repository(full_name)

        if not tracked_repo:
            return IngestionResult(
                repository=full_name,
                repository_id=None,
                status="ignored",
                indexed_commit_sha=None,
                is_incremental=False,
                message=f"Repository '{full_name}' is not tracked in DecisionGuard.",
            )

        # Match branch: payload ref format is 'refs/heads/<branch>'
        ref_header = payload.get("ref", "")
        branch = ref_header.removeprefix("refs/heads/") if ref_header.startswith("refs/heads/") else ref_header

        if branch != tracked_repo.tracked_branch:
            return IngestionResult(
                repository=full_name,
                repository_id=tracked_repo.id,
                status="ignored",
                indexed_commit_sha=tracked_repo.indexed_commit_sha,
                is_incremental=False,
                message=f"Push to branch '{branch}' ignored; tracked branch is '{tracked_repo.tracked_branch}'.",
            )

        after_sha = payload.get("after")
        if not after_sha or after_sha == "0000000000000000000000000000000000000000":
            return IngestionResult(
                repository=full_name,
                repository_id=tracked_repo.id,
                status="ignored",
                indexed_commit_sha=tracked_repo.indexed_commit_sha,
                is_incremental=False,
                message="Branch deletion event ignored.",
            )

        if after_sha == tracked_repo.indexed_commit_sha:
            return IngestionResult(
                repository=full_name,
                repository_id=tracked_repo.id,
                status="already_indexed",
                indexed_commit_sha=after_sha,
                is_incremental=True,
                message=f"Commit '{after_sha}' is already indexed.",
            )

        # Webhook flow: verified webhook -> github_repository_id -> tracked Repository -> Repository's GitHubConnection -> token
        cred = None
        if tracked_repo.github_connection_id:
            cred = self.metadata_store.get_credential_by_connection_id(tracked_repo.github_connection_id)
        elif tracked_repo.user_id:
            cred = self.metadata_store.get_credential_for_user(tracked_repo.user_id)

        if not cred:
            return IngestionResult(
                repository=full_name,
                repository_id=tracked_repo.id,
                status="failed",
                indexed_commit_sha=tracked_repo.indexed_commit_sha,
                is_incremental=False,
                message=f"No valid GitHub credential configured for tracked repository '{full_name}'.",
            )

        # Trigger incremental ingestion
        return await self.ingest_repository(
            repo_identifier=tracked_repo.full_name,
            branch=branch,
            credential=cred,
            user_id=tracked_repo.user_id,
            connection_id=tracked_repo.github_connection_id,
            trigger="webhook_push",
        )

    async def handle_github_pull_request_webhook(self, payload: dict[str, Any]) -> IngestionResult:
        """Acknowledge pull_request events (merged PRs are indexed via canonical branch push)."""
        action = payload.get("action")
        pr_data = payload.get("pull_request", {})
        merged = pr_data.get("merged", False)
        repo_data = payload.get("repository", {})
        full_name = repo_data.get("full_name", "unknown")
        github_repo_id = str(repo_data.get("id")) if repo_data.get("id") else None

        tracked_repo = self.metadata_store.get_repository(github_repo_id or full_name)
        repo_id = tracked_repo.id if tracked_repo else None

        if action == "closed" and merged:
            logger.info(
                f"PR #{pr_data.get('number')} was merged in {full_name}. "
                "Canonical RKG sync will be triggered by the corresponding branch push."
            )
            return IngestionResult(
                repository=full_name,
                repository_id=repo_id,
                status="completed",
                indexed_commit_sha=pr_data.get("merge_commit_sha"),
                is_incremental=False,
                message=f"Acknowledged merged PR #{pr_data.get('number')}. Sync handled by branch push event.",
            )

        return IngestionResult(
            repository=full_name,
            repository_id=repo_id,
            status="ignored",
            indexed_commit_sha=None,
            is_incremental=False,
            message=f"PR action '{action}' (merged={merged}) ignored for RKG synchronization.",
        )
