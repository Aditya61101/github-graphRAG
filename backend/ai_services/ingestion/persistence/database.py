from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, PullRequestRevisionModel

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "decisionguard.db"


def get_database_url() -> str:
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


_engine = None
_sessionmaker = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, echo=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessionmaker


def init_db() -> None:
    """Create all relational tables if they do not already exist."""
    engine = get_engine()
    with engine.begin() as conn:
        tables = inspect(conn).get_table_names()
        if tables and "app_schema_version" not in tables:
            raise RuntimeError(
                "SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_github_app"
            )
        if "app_schema_version" in tables:
            version = conn.exec_driver_sql(
                "SELECT version FROM app_schema_version"
            ).scalar()
            if version == 1:
                raise RuntimeError(
                    "SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_github_refresh"
                )
            if version == 2:
                raise RuntimeError(
                    "SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_onboarding"
                )
            if version != 3:
                raise RuntimeError("Unsupported application database schema version")
            if "onboarding_completed_at" not in {
                c["name"] for c in inspect(conn).get_columns("users")
            }:
                raise RuntimeError(
                    "Invalid schema v3: users.onboarding_completed_at is missing"
                )
            # Additive PR metadata table only; no existing columns are altered.
            PullRequestRevisionModel.__table__.create(conn, checkfirst=True)
        else:
            Base.metadata.create_all(conn)
            conn.exec_driver_sql(
                "CREATE TABLE app_schema_version (version INTEGER NOT NULL)"
            )
            conn.exec_driver_sql("INSERT INTO app_schema_version VALUES (3)")
        from .migrate_pr_orchestration import upgrade

        upgrade(conn)
