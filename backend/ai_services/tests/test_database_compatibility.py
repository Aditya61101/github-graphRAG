from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_services.ingestion.persistence import database
from ai_services.ingestion.persistence.models import Base, PullRequestRevisionModel, RepositoryModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore


@pytest.fixture(autouse=True)
def reset_engine(monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_sessionmaker", None)


def test_sqlite_default_location_and_connection_settings(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    mkdir = MagicMock()
    monkeypatch.setattr(type(database.DEFAULT_DB_PATH), "mkdir", mkdir)
    factory = MagicMock()
    monkeypatch.setattr(database, "create_engine", factory)
    assert database.get_engine() is factory.return_value
    url, = factory.call_args.args
    assert url.database == database.DEFAULT_DB_PATH.as_posix()
    assert database.DEFAULT_DB_PATH == Path(database.__file__).resolve().parents[3] / "data" / "decisionguard.db"
    assert factory.call_args.kwargs == {
        "connect_args": {"check_same_thread": False}, "echo": False, "hide_parameters": True,
    }
    database.get_engine()
    factory.assert_called_once()


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://user:private-password@localhost:5432/decisionguard",
    "postgresql+psycopg://user:private-password@/decisionguard?host=/cloudsql/project:region:instance",
])
def test_postgresql_engine_pool_and_socket_configuration(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    factory = MagicMock()
    monkeypatch.setattr(database, "create_engine", factory)
    database.get_engine()
    parsed, = factory.call_args.args
    assert parsed.drivername == "postgresql+psycopg"
    assert factory.call_args.kwargs == {
        "connect_args": {"connect_timeout": 10, "options": "-c timezone=UTC"}, "echo": False, "hide_parameters": True,
        "pool_size": 3, "max_overflow": 2, "pool_pre_ping": True, "pool_timeout": 30,
    }


@pytest.mark.parametrize("url", ["not-a-url-private-password", "postgresql://user:private-password@localhost/db"])
def test_invalid_database_configuration_does_not_expose_credentials(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(RuntimeError) as error:
        database.get_engine()
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__


def test_engine_factory_error_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(database, "create_engine", MagicMock(side_effect=ValueError("private-password")))
    with pytest.raises(RuntimeError) as error:
        database.get_engine()
    assert "private-password" not in str(error.value)


def revision():
    return SimpleNamespace(revision_key="revision", github_repository_id=101,
        pull_request_number=42, base_sha="a" * 40, head_sha="b" * 40, delivery_id="delivery")


def test_sqlite_real_duplicate_claim_retry_and_completion():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    try:
        Base.metadata.create_all(engine)
        store = SqliteApplicationStore(sessionmaker(bind=engine, expire_on_commit=False))
        with store.session_factory() as session:
            session.add(RepositoryModel(id="repo", github_repository_id="101", owner="owner", name="repo",
                full_name="owner/repo", repository_url="https://github.com/owner/repo"))
            session.commit()
        event = revision()
        assert store.claim_pr_revision("repo", event)
        assert not store.claim_pr_revision("repo", event)
        assert store.get_pr_revision(event.revision_key).attempts == 1
        for status, attempts in [("FAILED", 2), ("SUPERSEDED", 3)]:
            store.finish_pr_revision(event.revision_key, status, "error", "message")
            assert store.claim_pr_revision("repo", event)
            item = store.get_pr_revision(event.revision_key)
            assert item.status == "RUNNING" and item.attempts == attempts
            assert item.error is None and item.completed_at is None
            assert not store.claim_pr_revision("repo", event)
        store.finish_pr_revision(event.revision_key, "COMPLETED")
        assert not store.claim_pr_revision("repo", event)
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()], ids=["sqlite", "postgresql"])
@pytest.mark.parametrize("rowcounts,expected", [([1], True), ([0, 0], False), ([0, 1], True)])
def test_claim_sql_and_conditional_retry_for_both_dialects(dialect, rowcounts, expected):
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect.name
    # INSERT rowcount may be -1 with psycopg; RETURNING is authoritative.
    session.execute.side_effect = [SimpleNamespace(rowcount=-1,
        scalar_one_or_none=lambda n=n: 'revision' if n else None) for n in rowcounts]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    store = SqliteApplicationStore(factory)
    assert store.claim_pr_revision("repo", revision()) is expected
    statements = [call.args[0] for call in session.execute.call_args_list]
    compiled = statements[0].compile(dialect=dialect)
    assert "ON CONFLICT DO NOTHING" in str(compiled)
    returning_key = 'revision_key' if dialect.name == 'sqlite' else 'pull_request_revisions.revision_key'
    assert f'RETURNING {returning_key}' in str(compiled)
    assert compiled.params["revision_key"] == "revision"
    assert compiled.params["attempts"] == 1
    assert len(statements) == len(rowcounts)
    if len(statements) == 2:
        retry = statements[1].compile(dialect=dialect)
        assert "UPDATE pull_request_revisions" in str(retry)
        assert "pull_request_revisions.attempts +" in str(retry)
        assert ["FAILED", "SUPERSEDED"] in retry.params.values()
        assert "RUNNING" in retry.params.values()
        assert "revision" in retry.params.values()
    session.commit.assert_called_once()


def test_psycopg_driver_loads_without_connecting(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:private-password@localhost/decisionguard")
    engine = database.get_engine()
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.pool.checkedout() == 0
        factory = database.get_session_factory()
        assert factory.kw["bind"] is engine
        assert factory.kw["expire_on_commit"] is False
    finally:
        engine.dispose()
