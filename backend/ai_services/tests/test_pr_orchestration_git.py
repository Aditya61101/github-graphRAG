"""Actual isolated Git + production runtime; remote services are offline stubs."""

# ruff: noqa: F811 -- imported pytest fixtures are injected as test arguments.

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ai_services.decision_guard.evaluation import EvaluationResult, EvaluationSettings
from ai_services.decision_guard.evaluation.model_adapter import wire_text
from ai_services.decision_guard.evaluation.models import (
    AssessmentRequest,
    ChallengeRequest,
)
from ai_services.decision_guard.orchestration.models import ModelPacket
from ai_services.decision_guard.orchestration.runtime import ProductionRuntime
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.decision_guard.orchestration.worker import EvaluationWorker
from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.models import (
    Base,
    IngestionRunModel,
    RepositoryModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.pr.models import PullRequestEvent
from ai_services.ingestion.sources.github import GitHubRepositorySource
from ai_services.tests.test_decision_guard_context import Graph, changes, chunk, entity
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
)
from ai_services.tests.test_github_app import env as github_app_session  # noqa: F401 -- imported pytest fixture
from ai_services.tests.test_pr_ingestion import git, payload, repository  # noqa: F401 -- imported pytest fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_encoding,unavailable",
    [(None, False), ("o200k_base", False), ("o200k_base", True)],
)
async def test_real_production_worker_pinned_git_checkout_and_live_pr_normalization(
    repository, tmp_path, monkeypatch, token_encoding, unavailable
):
    engine = create_engine(f"sqlite:///{tmp_path / 'production-jobs.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    application = SqliteApplicationStore(sessions)
    event = repository.event
    with sessions.begin() as session:
        session.add(
            RepositoryModel(
                id="repo_101",
                github_repository_id="101",
                full_name="owner/repo",
                owner="owner",
                name="repo",
                repository_url="https://github.com/owner/repo",
                installation_id="10",
                status="COMPLETED",
                indexed_commit_sha=event.base_sha,
                tracked_branch="main",
            )
        )
        session.add(
            IngestionRunModel(
                id="run1",
                repository_id="repo_101",
                target_commit_sha=event.base_sha,
                status="COMPLETED",
            )
        )
    pr = {
        **payload(event)["pull_request"],
        "state": "open",
        "updated_at": "2026-10-16T01:00:00Z",
    }
    github = SimpleNamespace(
        store=application,
        indexing_credential=AsyncMock(return_value=repository.credential),
        installation_token=AsyncMock(return_value=repository.credential),
        request=AsyncMock(return_value=pr),
    )
    graph = Graph()
    graph.chunks = [
        {
            "path": "service.py",
            "chunk": chunk(text="first\nsecond\n", commit=event.base_sha),
            "entities": [entity()],
        }
    ]
    model = ScriptedModel()
    locks = {}
    runtime = ProductionRuntime(
        application,
        github,
        graph,
        lambda key: locks.setdefault(key, asyncio.Lock()),
        TrustedAuthority(),
        model,
    )
    store = EvaluationStore(sessions)
    settings = EvaluationSettings(
        max_tokens=200000,
        max_attempts=12,
        max_input_tokens=30000,
        token_encoding=token_encoding,
    )
    job_id = store.enqueue("repo_101", event, settings)
    paths = []
    original = GitHubRepositorySource._run_git

    def offline_git(source, args, **kwargs):
        args = [
            repository.remote.as_uri()
            if arg == "https://github.com/owner/repo.git"
            else arg
            for arg in args
        ]
        if args[0] == "init":
            paths.append(Path(args[1]))
        return original(source, args, **kwargs)

    monkeypatch.setattr(GitHubRepositorySource, "_run_git", offline_git)
    counters = []
    original_open = runtime.open

    @asynccontextmanager
    async def inspect_pipeline(job, event):
        async with original_open(job, event) as pipeline:
            assert pipeline.builder.token_counter is pipeline.token_counter
            assert pipeline.builder.request_budget.counter is pipeline.token_counter
            counters.append(pipeline.token_counter)
            yield pipeline

    runtime.open = inspect_pipeline
    if unavailable:
        import tiktoken

        def fail_encoding(name):
            raise ValueError("Tokenizer unavailable")

        monkeypatch.setattr(tiktoken, "get_encoding", fail_encoding)
    try:
        before = git(repository.root, "status", "--porcelain")
        await EvaluationWorker(store, runtime).run_job(job_id)
        job = store.get(job_id)
        if unavailable:
            assert job.status == "FAILED"
            assert job.error_code == "ValueError"
            assert job.context_json is None and job.result_json is None
            assert not model.requests and job.provider_attempts == 0
            assert paths and all(not path.exists() for path in paths)
            return
        assert job.status in {"COMPLETED", "DEFERRED"}, (job.status, job.error_code)
        result = EvaluationResult.model_validate_json(job.result_json)
        assert result.revision.base_sha == event.base_sha
        assert result.revision.head_sha == event.head_sha
        assert result.revision.merge_base_sha == repository.base
        assert result.plan and model.requests
        assert job.changes_json and job.context_json
        import json

        assert json.loads(job.context_json)["budget"]["estimator"] == (
            "model:o200k_base" if token_encoding else "conservative_utf8_bytes"
        )
        with sessions() as session:
            packets = list(
                session.scalars(select(ModelPacket).where(ModelPacket.job_id == job_id))
            )
            assert packets
            for packet in packets:
                schema = (
                    AssessmentRequest if packet.phase == "ASSESS" else ChallengeRequest
                )
                request = schema.model_validate_json(packet.request_json)
                expected = (
                    counters[0](wire_text(request, packet.phase))
                    + settings.provider_reserve
                )
                assert expected <= min(
                    settings.max_input_tokens,
                    settings.model_window - settings.max_output_tokens,
                )
                assert packet.reserved_tokens == expected + settings.max_output_tokens
        assert paths and all(not path.exists() for path in paths)
        assert git(repository.root, "status", "--porcelain") == before
        assert git(repository.root, "rev-parse", "HEAD") == event.base_sha
        assert github.request.await_args.args[1] == "/repos/owner/repo/pulls/42"
        github.indexing_credential.assert_awaited()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_production_pr_metadata_uses_scoped_installation_permissions_and_revocation(
    github_app_session, monkeypatch
):
    env = github_app_session
    selected = await env.service.select_repository(env.user.id, "owner/repo101")
    env.service.bind_repository(selected, env.user.id)
    event = PullRequestEvent.model_validate(
        {
            k: value
            for k, value in changes().model_dump().items()
            if k in PullRequestEvent.model_fields
        }
    ).model_copy(update={"repository_full_name": "owner/repo101"})
    response = {
        **payload(event)["pull_request"],
        "state": "open",
        "updated_at": "2026-10-16T01:00:00Z",
    }
    original = env.service.request

    async def request(method, path, token, **kwargs):
        if path == "/repos/owner/repo101/pulls/42":
            assert token.startswith("opaque-installation-token-")
            return response
        return await original(method, path, token, **kwargs)

    monkeypatch.setattr(env.service, "request", request)
    runtime = ProductionRuntime(env.store, env.service, None, None, None, None)
    assert (await runtime.current_event(event)).revision_key == event.revision_key
    import json

    scopes = [
        json.loads(call.content)
        for call in env.state["calls"]
        if call.url.path.endswith("/access_tokens")
    ]
    assert {
        "repository_ids": [101],
        "permissions": {
            "contents": "read",
            "metadata": "read",
            "pull_requests": "read",
        },
    } in scopes
    env.state["inventory"][10] = []
    with pytest.raises(GitHubAppError):
        await runtime.current_event(event)
