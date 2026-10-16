"""Focused lifecycle/failure tests; SQLite is explicitly not PostgreSQL proof."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from ai_services.ingestion.persistence.models import (
    Base, RepositoryModel, IngestionRunModel, RepositoryIngestionState,
)
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs, LostIngestionLease
from ai_services.ingestion.graph_control import GraphControl
from ai_services.ingestion.readiness import GraphReadBarrier, GraphUnavailable
from ai_services.ingestion.rkg.neo4j_writer import Neo4jRepositoryWriter
from ai_services.ingestion.sources.github import GitHubRepositorySource
from ai_services.ingestion.sources.interface import RepositoryRef
from ai_services.tests.test_pr_ingestion import git

A, B = "a"*40, "b"*40


@pytest.fixture
def jobs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'jobs.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as s:
        s.add(RepositoryModel(id="repo", github_repository_id="101", owner="owner",
            name="repo", full_name="owner/repo", repository_url="https://github.com/owner/repo",
            tracked_branch="main", installation_id="10"))
    clock = [1000]
    store = IngestionJobs(sessions, clock=lambda:clock[0], lease_seconds=120)
    store.test_clock = clock
    yield store
    engine.dispose()


def enqueue(jobs, sha=A, **kwargs):
    return jobs.enqueue("repo", sha, branch="main", installation_id="10", **kwargs)


def test_acceptance_is_persistent_before_any_dispatch_and_duplicate_delivery(jobs):
    first = enqueue(jobs, delivery_id="delivery")
    assert jobs.get(first).status == "QUEUED"
    assert jobs.due() == [first]
    assert enqueue(jobs, delivery_id="delivery") == first
    with pytest.raises(ValueError, match="identity"):
        enqueue(jobs, B, delivery_id="delivery")


def test_exclusive_claim_and_stale_completion_cannot_regress(jobs):
    a, b = enqueue(jobs), enqueue(jobs, B)
    owner_a = jobs.claim(a)
    assert jobs.claim(b) is None
    assert jobs.claim(a) is None
    jobs.test_clock[0] += 121
    owner_b = jobs.claim(b)
    assert owner_b.token > owner_a.token
    jobs.finish(b, owner_b.owner, B)
    with pytest.raises(LostIngestionLease):
        jobs.finish(a, owner_a.owner, A)
    with jobs.sessions() as s:
        assert s.get(RepositoryModel, "repo").indexed_commit_sha == B


def test_abandoned_same_run_retries_full_and_does_not_poison_ready(jobs):
    run_id = enqueue(jobs)
    first = jobs.claim(run_id)
    jobs.test_clock[0] += 121
    retry = jobs.claim(run_id)
    assert retry.token > first.token and retry.force_full
    jobs.finish(run_id, retry.owner, A)
    with jobs.sessions() as s:
        state = s.get(RepositoryIngestionState, "repo")
        assert state.state == "READY" and state.active_run_id is None


def test_bounded_retries_and_sanitized_failure(jobs):
    run_id = enqueue(jobs)
    for _ in range(3):
        run = jobs.claim(run_id)
        jobs.fail(run_id, run.owner, "ProviderFailure")
        jobs.test_clock[0] += 301
    assert jobs.get(run_id).status == "FAILED"
    assert run_id not in jobs.due()
    assert jobs.get(run_id).error == "ProviderFailure"


def test_graph_seal_can_recover_missing_sql_publication(jobs):
    run_id = enqueue(jobs)
    run = jobs.claim(run_id)
    jobs.test_clock[0] += 121
    assert jobs.recover_sealed(run_id, dict(state="COMPLETE", token=run.token,
        run_id=run.id, target=A))
    assert jobs.get(run_id).status == "COMPLETED"
    assert not jobs.recover_sealed(run_id, dict(state="COMPLETE", token=run.token+1,
        run_id=run.id, target=A))


@pytest.mark.asyncio
async def test_readers_reject_dirty_graph_and_epoch_change(jobs):
    run_id = enqueue(jobs)
    control = SimpleNamespace(snapshot=lambda repo:dict(state="COMPLETE",
        run_id=run_id, token=1, target=A))
    barrier = GraphReadBarrier(jobs.sessions, control)
    with pytest.raises(GraphUnavailable):
        barrier.snapshot("repo")
    run = jobs.claim(run_id)
    jobs.finish(run_id, run.owner, A)
    with pytest.raises(GraphUnavailable):
        async with barrier.read("repo"):
            newer = enqueue(jobs, B)
            assert jobs.claim(newer)


def test_stale_graph_writer_rejected_under_lock():
    control = GraphControl(MagicMock())
    tx = MagicMock()
    tx.run.return_value = []  # No current live token matches.
    @asynccontextmanager
    async def unused(): yield
    from contextlib import contextmanager
    @contextmanager
    def locked(repo):
        yield tx
    control._locked = locked
    run = SimpleNamespace(repository_id="repo", token=1, owner="old", id="run")
    with pytest.raises(LostIngestionLease):
        with control.transaction(run):
            pytest.fail("Stale worker reached mutation")


@pytest.mark.asyncio
async def test_cold_workspace_is_pinned_and_removed(tmp_path):
    origin = tmp_path/"origin"
    origin.mkdir()
    git(origin, "init")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    (origin/"a.py").write_text("old")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "old")
    old = git(origin, "rev-parse", "HEAD").strip()
    (origin/"a.py").write_text("new")
    git(origin, "commit", "-am", "new")
    new = git(origin, "rev-parse", "HEAD").strip()
    source = GitHubRepositorySource(tmp_path/"unused-clones")
    original = source._run_git
    def local(args, **kwargs):
        args = [str(origin) if str(a).startswith("https://github.com/") else a for a in args]
        return original(args, **kwargs)
    source._run_git = local
    async with source.open_workspace(RepositoryRef("owner", "repo", branch="main"), old) as (snap, available):
        root = snap.root
        assert snap.commit_sha == old and (root/"a.py").read_text() == "old"
    assert not root.exists()
    async with source.open_workspace(RepositoryRef("owner", "repo", branch="main"), new, old) as (snap, available):
        assert available
        diff = source.compute_diff(snap, old, new)
        assert [d.path for d in diff] == ["a.py"]
    assert not source._repo_clone_path(RepositoryRef("owner", "repo")).exists()


def test_cleanup_has_independent_assertion_edge_and_entity_phases():
    driver = MagicMock()
    writer = Neo4jRepositoryWriter(driver)
    writer.delete_files("repo", ["gone.py"])
    calls = [c.args[0] for c in driver.execute_query.call_args_list]
    assert len(calls) == 4
    assert "source_type" in calls[1] and "SUPPORTS" in calls[1]
    assert "DELETE r" in calls[2] and "ASSERTS" in calls[2]
    assert "AFFECTS|CONSTRAINS" in calls[3] and "MENTIONS" in calls[3]
    assert "UNWIND assertions" not in calls[3]


@pytest.mark.asyncio
async def test_community_failure_is_required():
    from ai_services.ingestion.service import RepositoryIngestionService
    service = RepositoryIngestionService(MagicMock(), MagicMock(), MagicMock())
    with pytest.raises(RuntimeError, match="Required community"):
        await service._run_community_pipeline("repo")


@pytest.mark.asyncio
async def test_leiden_uses_scoped_stream_and_canonical_ids():
    from ai_services.ingestion.community.detector import GDSLeidenDetector
    from ai_services.ingestion.community.config import CommunityConfig
    driver = MagicMock()
    driver.execute_query.return_value.records = [
        {"raw_community_id":0, "entity_ids":["canonical-a", "canonical-b"]}]
    result = await GDSLeidenDetector(CommunityConfig(repository_id="repo")).detect(driver, "unique")
    query = driver.execute_query.call_args.args[0]
    assert "leiden.stream" in query and "e.repository=$repo" in query
    assert "collect(e.id)" in query and "leiden.write" not in query
    assert result.node_count == 2


@pytest.mark.asyncio
async def test_worker_partial_failure_retries_full_then_recovers_sealed_sql_failure(jobs, monkeypatch):
    from ai_services.ingestion.worker import IngestionWorker
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING", "true")
    run_id = enqueue(jobs)
    seal = {}
    def arm(run):
        seal.update(state="BUILDING", token=run.token, run_id=run.id, target=run.target_commit_sha)
    control = SimpleNamespace(snapshot=lambda _:dict(seal) if seal else None, arm=arm)
    worker = IngestionWorker(jobs, None, None, None, control, llm=None, embedder=None)
    async def partial(run):
        assert run.status == "INDEXING"
        raise RuntimeError("synthetic crash after source write")
    worker._execute = partial
    await worker.run(run_id)
    assert jobs.get(run_id).status == "RETRY"
    jobs.test_clock[0] += 301
    async def sealed(run):
        assert run.force_full and run.attempts == 2
        seal["state"] = "COMPLETE"
        raise RuntimeError("synthetic SQL commit outage")
    worker._execute = sealed
    await worker.run(run_id)
    assert jobs.get(run_id).status == "INDEXING"
    await worker.run(run_id)
    assert jobs.get(run_id).status == "COMPLETED"


def test_production_maintenance_writer_cannot_bypass_fence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/unused")
    driver = MagicMock()
    with pytest.raises(RuntimeError, match="requires owned"):
        Neo4jRepositoryWriter(driver).reconcile_code("repo")
    driver.execute_query.assert_not_called()


def test_repeated_hard_death_exhaustion_exposes_manual_retry(jobs):
    run_id = enqueue(jobs)
    for _ in range(3):
        assert jobs.claim(run_id)
        jobs.test_clock[0] += 121
    assert jobs.claim(run_id) is None
    assert jobs.get(run_id).status == "FAILED"
    with jobs.sessions() as session:
        assert session.get(RepositoryModel, "repo").status == "FAILED"
    fresh = enqueue(jobs)
    assert fresh != run_id and jobs.claim(fresh).force_full
