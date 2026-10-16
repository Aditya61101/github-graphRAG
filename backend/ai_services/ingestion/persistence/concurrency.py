"""Cooperative serialization of short application metadata write transactions."""
from sqlalchemy import text

# Distinct from the schema initializer lock. No network/LLM/Git work may run
# while holding this lock. All protected writers use the same lock order.
METADATA_WRITE_LOCK = 4920260303


def serialize_metadata_write(session):
    """Acquire before any reads; commit/rollback releases the transaction lock.

    A coarse lock deliberately avoids new constraints/leases in schema v3.
    SQLite's reserved write lock also closes read-then-insert races locally.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '10s'"))
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": METADATA_WRITE_LOCK})
    elif dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError("Metadata writes require SQLite or PostgreSQL")
