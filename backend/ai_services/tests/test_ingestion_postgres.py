"""Opt-in real PostgreSQL migration and independent-process ownership test."""
import concurrent.futures
import json
import os
import subprocess
import sys
import pytest
from sqlalchemy import create_engine, inspect, MetaData, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from ai_services.ingestion.persistence import postgres_schema as schema
from ai_services.ingestion.persistence.models import Base, RepositoryModel, UserModel, IngestionRunModel
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs, LostIngestionLease


def test_real_postgres_ingestion_migration_and_process_ownership(monkeypatch):
    url = os.getenv("DECISIONGUARD_INGESTION_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires an explicitly provisioned empty local ingestion test database")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 55439
    assert parsed.database.startswith("decisionguard_integration_test_stage_b")
    engine = create_engine(url)
    try:
        assert not inspect(engine).get_table_names(), "Refuse existing database"
        legacy = MetaData()
        excluded = {t.name for t in schema.COMPONENT_TABLES} | {
            "repository_ingestion_state", "ingestion_deliveries"}
        for table in Base.metadata.sorted_tables:
            if table.name not in excluded:
                table.to_metadata(legacy)
        from ai_services.ingestion.persistence.migrate_ingestion import EXTRA_COLUMNS
        for name in EXTRA_COLUMNS:
            legacy.tables["ingestion_runs"]._columns.remove(legacy.tables["ingestion_runs"].c[name])
        with engine.begin() as conn:
            legacy.create_all(conn)
            schema.VERSION_TABLE.create(conn)
            conn.execute(schema.VERSION_TABLE.insert().values(version=3))
            conn.execute(legacy.tables["users"].insert().values(id="user", username="local-test"))
            conn.execute(legacy.tables["github_installations"].insert().values(id="10",
                app_id="app", account_id="account", account_login="owner", account_type="User",
                status="ACTIVE", repository_selection="all"))
            conn.execute(legacy.tables["repositories"].insert().values(id="repo", owner="owner", name="repo",
                github_repository_id="101", full_name="owner/repo", repository_url="https://github.com/owner/repo",
                installation_id="10", tracked_branch="main", user_id="user"))
            conn.execute(legacy.tables["ingestion_runs"].insert().values(id="historical", repository_id="repo",
                target_commit_sha="a"*40, status="COMPLETED"))
        schema.upgrade_postgresql_schema(engine)
        schema.upgrade_postgresql_schema(engine)
        schema.validate_postgresql_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        jobs = IngestionJobs(sessions)
        with sessions() as s:
            assert s.get(UserModel, "user").username == "local-test"
            assert s.get(IngestionRunModel, "historical").status == "COMPLETED"
        ids = [jobs.enqueue("repo", sha*40, branch="main", installation_id="10") for sha in ("b","c")]
        code = """
import json,os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
j=IngestionJobs(sessionmaker(create_engine(os.environ['DECISIONGUARD_INGESTION_TEST_POSTGRES_URL']),expire_on_commit=False))
r=j.claim(os.environ['INGESTION_TEST_RUN'])
print(json.dumps({'id':r.id,'owner':r.owner,'token':r.token} if r else None))
"""
        def claim(run_id):
            env = dict(os.environ, INGESTION_TEST_RUN=run_id)
            result = subprocess.run([sys.executable, "-c", code], env=env,
                capture_output=True, text=True, check=True, timeout=60)
            return json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ids))
        owners = [r for r in results if r]
        assert len(owners) == 1
        first = owners[0]
        # Simulate dead owner using synthetic test rows; never wait on or modify production.
        with sessions.begin() as s:
            s.get(IngestionRunModel, first["id"]).lease_until = 0
        other_id = next(i for i in ids if i != first["id"])
        newer = jobs.claim(other_id)
        assert newer.token > first["token"] and newer.force_full
        jobs.finish(newer.id, newer.owner, newer.target_commit_sha)
        with pytest.raises(LostIngestionLease):
            jobs.finish(first["id"], first["owner"], "b"*40)
        with sessions() as s:
            assert s.get(RepositoryModel,"repo").indexed_commit_sha == newer.target_commit_sha
        # The PR outbox coexists with the repository queue in this upgraded schema.
        # Independent processes must share Session B's global admission limit.
        from ai_services.decision_guard.orchestration.store import EvaluationStore, LostLease
        from ai_services.decision_guard.orchestration.models import EvaluationJob, DispatchIntent
        from ai_services.ingestion.pr.models import PullRequestEvent
        from ai_services.tests.test_processing_gate import event_payload
        monkeypatch.setenv("PR_EVALUATION_MAX_CONCURRENCY", "1")
        evaluations = EvaluationStore(sessions)
        event = PullRequestEvent.from_webhook(event_payload(), "combined-first")
        alternate = event.model_copy(update={"pull_request_number": 99, "delivery_id": "combined-second"})
        evaluation_ids = [evaluations.enqueue("repo", e) for e in (event, alternate)]
        with sessions() as s:
            assert all(s.get(DispatchIntent, job_id) is not None for job_id in evaluation_ids)
        code = """
import json,os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from ai_services.decision_guard.orchestration.store import EvaluationStore
j=EvaluationStore(sessionmaker(create_engine(os.environ['DECISIONGUARD_INGESTION_TEST_POSTGRES_URL']),expire_on_commit=False))
print(json.dumps(j.claim(os.environ['INGESTION_TEST_RUN'])))
"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, evaluation_ids))
        assert sum(token is not None for token in tokens) == 1
        claimed_index = next(i for i, token in enumerate(tokens) if token)
        claimed_id = evaluation_ids[claimed_index]
        with sessions.begin() as s:
            s.get(EvaluationJob, claimed_id).lease_until = 0
        assert evaluations.claim(evaluation_ids[1-claimed_index])
        with pytest.raises(LostLease):
            evaluations.heartbeat(claimed_id, tokens[claimed_index])
        with sessions() as s:
            assert s.get(IngestionRunModel, "historical").status == "COMPLETED"
            assert s.get(RepositoryModel, "repo").indexed_commit_sha == newer.target_commit_sha
    finally:
        engine.dispose()
