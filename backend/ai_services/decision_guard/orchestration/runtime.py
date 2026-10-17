"""Real GitHub/isolated Git -> production Stage 4.1 -> hardened Stage 4.2."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.context import (
    ArchitectureContextBuilder,
    ContextConstructionError,
    conservative_tokens,
)
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.models import DispatchDenied
from ai_services.decision_guard.graph import Neo4jArchitectureGraphReader
from ai_services.decision_guard.models import BaselineSnapshot
from ai_services.decision_guard.source import GitRevisionSourceReader
from ai_services.ingestion.persistence.models import IngestionRunModel, RepositoryModel
from ai_services.ingestion.pr.git_reader import PullRequestGitReader
from ai_services.ingestion.pr.models import PullRequestChangeSet, PullRequestEvent
from ai_services.ingestion.pr.settings import PullRequestSettings
from ai_services.ingestion.sources.github import GitHubRepositorySource

logger = logging.getLogger(__name__)


async def drain_thread(function, *args):
    """Do not delete temporary files while a cancelled Git/read thread uses them."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception as error:  # noqa: BLE001 -- drain thread and preserve cancellation
            logger.debug(
                "Cancelled source operation ended error_type=%s", type(error).__name__
            )
        raise


class DatabaseReadiness:
    def __init__(self, sessions, control=None):
        self.sessions, self.control = sessions, control

    async def snapshot(self, repository_id):
        if self.control is not None:
            from ai_services.ingestion.readiness import GraphReadBarrier, GraphUnavailable
            try:
                epoch, run_id, token, sha = GraphReadBarrier(self.sessions, self.control).snapshot(repository_id)
            except GraphUnavailable:
                raise ContextConstructionError("Repository graph update or repair is in progress") from None
            with self.sessions() as session:
                repo = session.get(RepositoryModel, repository_id)
                return BaselineSnapshot(repository_id=repo.id,
                    github_repository_id=repo.github_repository_id, tracked_branch=repo.tracked_branch,
                    indexed_rkg_sha=sha, repository_status="COMPLETED", readiness="READY",
                    graph_generation=epoch, ingestion_run_id=run_id, unfinished_ingestion=False)
        # Existing ingestion commits INDEXING before graph mutation, and only
        # commits COMPLETED/indexed SHA after graph publication. Failed or
        # abandoned runs remain fail-closed; never infer readiness from SHA alone.
        with self.sessions() as session:
            from ai_services.ingestion.readiness import pending_adr_deletion
            if pending_adr_deletion(session, repository_id):
                raise ContextConstructionError('ADR deletion cleanup is pending')
            repo = session.get(RepositoryModel, repository_id)
            runs = list(
                session.scalars(
                    select(IngestionRunModel)
                    .where(IngestionRunModel.repository_id == repository_id)
                    .order_by(IngestionRunModel.started_at, IngestionRunModel.id)
                )
            )
            if not repo or not repo.indexed_commit_sha or not runs:
                raise ContextConstructionError(
                    "Initial accepted repository ingestion must complete first"
                )
            latest = runs[-1]
            unfinished = any(run.status == "INDEXING" for run in runs)
            ready = (
                repo.status == "COMPLETED"
                and latest.status == "COMPLETED"
                and latest.target_commit_sha == repo.indexed_commit_sha
                and not unfinished
            )
            return BaselineSnapshot(
                repository_id=repo.id,
                github_repository_id=repo.github_repository_id,
                tracked_branch=repo.tracked_branch,
                indexed_rkg_sha=repo.indexed_commit_sha,
                repository_status=repo.status,
                readiness="READY" if ready else "DIRTY",
                graph_generation=len(runs),
                ingestion_run_id=latest.id,
                unfinished_ingestion=unfinished,
            )


class ProductionRuntime:
    def __init__(
        self, application_store, github_app, graph, repository_lock, authority, model
    ):
        self.application_store, self.github_app, self.graph = (
            application_store,
            github_app,
            graph,
        )
        self.repository_lock, self.authority, self.model = (
            repository_lock,
            authority,
            model,
        )
        from ai_services.ingestion.graph_control import GraphControl
        self.readiness = DatabaseReadiness(application_store.session_factory,
            GraphControl(graph.driver, graph.database)
            if isinstance(graph, Neo4jArchitectureGraphReader) else None)

    async def current_event(self, event):
        repo = self.application_store.get_repository(str(event.github_repository_id))
        if (
            not repo
            or repo.installation_id != str(event.installation_id)
            or repo.full_name.lower() != event.repository_full_name.lower()
        ):
            raise ValueError("Repository installation binding changed")
        from .projections import synchronize
        from .store import EvaluationStore

        pr = await synchronize(
            EvaluationStore(self.application_store.session_factory),
            self.github_app,
            repo.id,
            event.pull_request_number,
        )
        if pr.get("state") != "open":
            return None
        # Reuse the webhook normalizer for API-fetched metadata, including forks.
        return PullRequestEvent.from_webhook(
            {
                "installation": {"id": int(repo.installation_id)},
                "repository": {
                    "id": int(repo.github_repository_id),
                    "full_name": repo.full_name,
                    "private": False,
                },
                "pull_request": pr,
                "action": "synchronize",
            },
            None,
        )

    @asynccontextmanager
    async def open(self, job, event):
        repo = self.application_store.get_repository(job.repository_id)
        credential = await self.github_app.indexing_credential(
            repo.id, str(event.installation_id)
        )
        with TemporaryDirectory(prefix="decisionguard-pr-") as temporary:
            source = GitHubRepositorySource(temporary)
            root = Path(temporary) / "repository"
            # Object-only isolated checkout. No shared clone, reset, hooks,
            # submodule initialization, PR code execution or token in the URL.
            await drain_thread(source._run_git, ["init", str(root)])
            reader = PullRequestGitReader(source, PullRequestSettings.from_env())
            merge, files = await drain_thread(
                reader.read_revision,
                root,
                event,
                credential,
                f"https://github.com/{repo.full_name}.git",
            )
            revision_source = GitRevisionSourceReader(repo.id, root, reader)
            settings = EvaluationSettings.model_validate_json(job.settings_json)
            token_counter = conservative_tokens
            if settings.token_encoding:
                import tiktoken

                token_counter = ModelTokenCounter.from_encoding(
                    tiktoken.get_encoding(settings.token_encoding),
                    model=settings.token_encoding,
                )
            budget = ContextRequestBudget(
                revision_source,
                self.authority,
                settings=settings,
                token_counter=token_counter,
            )
            builder = ArchitectureContextBuilder(
                self.graph,
                revision_source,
                self.readiness,
                self.repository_lock,
                request_budget=budget,
                token_counter=token_counter,
            )
            yield ProductionPipeline(
                event,
                repo.id,
                merge,
                files,
                builder,
                revision_source,
                self.authority,
                settings,
                token_counter,
            )


class ProductionPipeline:
    def __init__(
        self,
        event,
        repository_id,
        merge,
        files,
        builder,
        source,
        authority,
        settings,
        token_counter=conservative_tokens,
    ):
        self.event, self.repository_id, self.merge, self.files = (
            event,
            repository_id,
            merge,
            files,
        )
        self.token_counter = token_counter
        self.builder, self.source, self.authority, self.settings = (
            builder,
            source,
            authority,
            settings,
        )

    async def ingest(self):
        return PullRequestChangeSet(
            **self.event.model_dump(),
            repository_id=self.repository_id,
            merge_base_sha=self.merge,
            files=self.files,
        )

    async def context(self, changes):
        return await self.builder.build(changes)

    async def evaluate(self, context, model):
        return await DecisionIntegrityEvaluator(
            model,
            settings=self.settings,
            source=self.source,
            authority=self.authority,
            token_counter=self.token_counter,
        ).evaluate(context)


def production_runtime(app, *, deployment_revision):
    from api_services.app.config import ADRS_STORAGE_DIR

    return ProductionRuntime(
        app.state.sqlite_store,
        app.state.github_app,
        Neo4jArchitectureGraphReader(
            app.state.deps.driver, database=os.environ.get("NEO4J_DATABASE") or "neo4j"
        ),
        app.state.ingestion_service._get_lock,
        UploadAuthority(app.state.sqlite_store, ADRS_STORAGE_DIR),
        ConfiguredAzureModel(
            app.state.deps.llm,
            deployment_revision=deployment_revision or "UNCONFIGURED",
        ),
    )


class ConfiguredAzureModel(AzureEvaluationModel):
    @property
    def dispatch_allowed(self):
        return self.deployment_revision != "UNCONFIGURED"

    async def _call(self, request, phase, max_output_tokens, timeout):
        if not self.dispatch_allowed:
            raise DispatchDenied("AUTHORIZATION_DENIED")
        return await super()._call(request, phase, max_output_tokens, timeout)
