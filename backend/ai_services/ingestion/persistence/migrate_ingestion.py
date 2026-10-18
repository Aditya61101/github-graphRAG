"""Additive ingestion lifecycle migration; no history or graph data is rewritten."""
from sqlalchemy import inspect, text, Column, Integer, MetaData, Table
from .models import RepositoryIngestionState, IngestionDelivery

version_table = Table("ingestion_schema_version", MetaData(),
                      Column("version", Integer, nullable=False))
EXTRA_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at": "INTEGER NOT NULL DEFAULT 0",
    "owner": "VARCHAR(64)", "token": "INTEGER", "lease_until": "INTEGER",
    "force_full": "INTEGER NOT NULL DEFAULT 0",
    "branch": "VARCHAR(100)", "installation_id": "VARCHAR(64)",
}


def upgrade(conn):
    columns = {c["name"] for c in inspect(conn).get_columns("ingestion_runs")}
    for name, ddl in EXTRA_COLUMNS.items():
        if name not in columns:
            conn.execute(text(f"ALTER TABLE ingestion_runs ADD COLUMN {name} {ddl}"))
    RepositoryIngestionState.__table__.create(conn, checkfirst=True)
    IngestionDelivery.__table__.create(conn, checkfirst=True)
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ingestion_due "
                      "ON ingestion_runs(status,next_attempt_at)"))
    version_table.create(conn, checkfirst=True)
    versions = conn.execute(version_table.select()).scalars().all()
    if not versions:
        conn.execute(version_table.insert().values(version=1))
    elif versions != [1]:
        raise RuntimeError("Unsupported ingestion schema version")
