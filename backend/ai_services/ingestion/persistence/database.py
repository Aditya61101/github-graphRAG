from __future__ import annotations

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
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
        try:
            url = make_url(get_database_url())
            options = {"connect_args": {}, "echo": False, "hide_parameters": True}
            if url.get_backend_name() == "sqlite":
                options["connect_args"] = {"check_same_thread": False}
            elif url.get_backend_name() == "postgresql":
                if url.drivername != "postgresql+psycopg":
                    raise ValueError("PostgreSQL requires the psycopg dialect")
                options.update(
                    pool_size=3, max_overflow=2, pool_pre_ping=True,
                    pool_timeout=30, connect_args={"connect_timeout": 10},
                )
            _engine = create_engine(url, **options)
        except (SQLAlchemyError, ValueError, ImportError):
            # Parser/driver errors can include the supplied URL. Do not expose it.
            raise RuntimeError(
                "Unable to configure application database; check DATABASE_URL "
                "and the installed driver (PostgreSQL requires postgresql+psycopg)."
            ) from None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessionmaker


def init_db() -> None:
    """Initialize local SQLite; validate explicitly initialized PostgreSQL."""
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        from .postgres_schema import validate_postgresql_schema
        validate_postgresql_schema(engine)
        return
    with engine.begin() as conn:
        tables = inspect(conn).get_table_names()
        if tables and 'app_schema_version' not in tables:
            raise RuntimeError('SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_github_app')
        if 'app_schema_version' in tables:
            version = conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar()
            if version == 1:
                raise RuntimeError('SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_github_refresh')
            if version == 2:
                raise RuntimeError('SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_onboarding')
            if version != 3:
                raise RuntimeError('Unsupported application database schema version')
            if 'onboarding_completed_at' not in {c['name'] for c in inspect(conn).get_columns('users')}:
                raise RuntimeError('Invalid schema v3: users.onboarding_completed_at is missing')
            # Additive PR metadata table only; no existing columns are altered.
            PullRequestRevisionModel.__table__.create(conn, checkfirst=True)
        else:
            Base.metadata.create_all(conn)
            conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
            conn.exec_driver_sql('INSERT INTO app_schema_version VALUES (3)')
