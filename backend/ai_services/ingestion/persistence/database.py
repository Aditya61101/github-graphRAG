from __future__ import annotations

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

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
        if tables and 'app_schema_version' not in tables:
            raise RuntimeError('SQLite upgrade required: run python -m ai_services.ingestion.persistence.migrate_github_app')
        if 'app_schema_version' in tables:
            if conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar() != 1:
                raise RuntimeError('Unsupported application database schema version')
        else:
            Base.metadata.create_all(conn)
            conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
            conn.exec_driver_sql('INSERT INTO app_schema_version VALUES (1)')
