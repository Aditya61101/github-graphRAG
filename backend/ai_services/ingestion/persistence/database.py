from __future__ import annotations

import os
from pathlib import Path
from sqlalchemy import create_engine
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
    Base.metadata.create_all(bind=engine)
    # Ensure user_id column exists on repositories if table existed previously in SQLite
    with engine.begin() as conn:
        try:
            res = conn.exec_driver_sql("PRAGMA table_info(repositories)").fetchall()
            cols = [r[1] for r in res]
            if cols and "user_id" not in cols:
                conn.exec_driver_sql("ALTER TABLE repositories ADD COLUMN user_id VARCHAR(64) REFERENCES users(id)")
        except Exception:
            pass
