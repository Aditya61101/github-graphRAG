"""Opt-in tests for a NEW disposable local PostgreSQL database only.

Never supply the application DATABASE_URL. These tests add schema and synthetic
rows; they never drop tables, reset databases or contact external services.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.ingestion.persistence import postgres_schema as schema
from ai_services.ingestion.persistence.models import Base, UserModel, RepositoryModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.pr.models import PullRequestEvent
from ai_services.tests.test_processing_gate import event_payload


@pytest.mark.integration
def test_real_postgres_core_upgrade_and_durable_claims(monkeypatch):
    url = os.getenv('DECISIONGUARD_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Requires an explicitly provisioned disposable local PostgreSQL database')
    parsed = make_url(url)
    assert parsed.host == '127.0.0.1' and parsed.port == 55439
    assert parsed.database.startswith('decisionguard_integration_test')
    from ai_services.ingestion.persistence import database
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setattr(database, '_engine', None)
    engine = database.get_engine()
    try:
        with engine.connect() as connection:
            assert connection.scalar(text('SHOW timezone')) == 'UTC'
        # Refuse an existing schema, even if the test URL was misconfigured.
        assert not inspect(engine).get_table_names(), 'Test requires an empty disposable database'
        components = {t.name for t in schema.COMPONENT_TABLES}
        with engine.begin() as connection:
            Base.metadata.create_all(connection, tables=[
                t for t in Base.metadata.sorted_tables if t.name not in components])
            schema.VERSION_TABLE.create(connection)
            connection.execute(schema.VERSION_TABLE.insert().values(version=3))
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions.begin() as session:
            session.add(UserModel(id='test-user', username='integration'))
            session.flush()
            session.add(RepositoryModel(id='repo', github_repository_id='101', user_id='test-user',
                owner='owner', name='repo101', full_name='owner/repo101',
                repository_url='https://github.com/owner/repo101'))
        with pytest.raises(schema.PostgreSQLSchemaError, match='--upgrade'):
            schema.validate_postgresql_schema(engine)
        schema.upgrade_postgresql_schema(engine)
        schema.upgrade_postgresql_schema(engine)
        schema.validate_postgresql_schema(engine)
        with sessions() as session:
            assert session.get(UserModel, 'test-user').username == 'integration'
            assert session.scalar(select(schema.VERSION_TABLE.c.version)) == 3
            for marker, version in schema.VERSION_TABLES[1:]:
                assert session.scalars(select(marker.c.version)).all() == [version]
        store = EvaluationStore(sessions)
        event = PullRequestEvent.from_webhook(event_payload(), 'pg-local-delivery')
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: store.enqueue('repo', event), range(2)))
        assert jobs[0] == jobs[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: store.claim(jobs[0]), range(2)))
        assert sum(token is not None for token in claims) == 1
        assert store.get(jobs[0]).attempts == 1
        application = SqliteApplicationStore(sessions)
        with ThreadPoolExecutor(max_workers=2) as pool:
            revisions = list(pool.map(lambda _: application.claim_pr_revision('repo', event), range(2)))
        assert sorted(revisions) == [False, True]
        application.finish_pr_revision(event.revision_key, 'FAILED')
        assert application.claim_pr_revision('repo', event)
        assert application.get_pr_revision(event.revision_key).attempts == 2
        application.finish_pr_revision(event.revision_key, 'COMPLETED')
        assert not application.claim_pr_revision('repo', event)
    finally:
        engine.dispose()
