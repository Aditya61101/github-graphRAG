"""Add only ADR execution mapping and its version marker; never rewrite graph data."""
from sqlalchemy import Column, Integer, MetaData, Table
from .models import ADRIngestionRun

version_table = Table("adr_ingestion_schema_version", MetaData(),
                      Column("version", Integer, nullable=False))


def upgrade(conn):
    ADRIngestionRun.__table__.create(conn, checkfirst=True)
    version_table.create(conn, checkfirst=True)
    versions = conn.execute(version_table.select()).scalars().all()
    if not versions:
        conn.execute(version_table.insert().values(version=1))
    elif versions != [1]:
        raise RuntimeError("Unsupported ADR ingestion schema version")
