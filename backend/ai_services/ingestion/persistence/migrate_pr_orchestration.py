"""Additive, transactional Stage 4.3 migration (application schema v3 is preserved).

Run from backend: python -m ai_services.ingestion.persistence.migrate_pr_orchestration
"""

from sqlalchemy import Column, Integer, MetaData, Table, inspect, select

from ai_services.decision_guard.orchestration.models import TABLES

version_table = Table(
    "pr_orchestration_schema_version",
    MetaData(),
    Column("version", Integer, nullable=False),
)


def upgrade(connection):
    # Portable schema; no ALTER, destructive downgrade or source-data rewrite.
    # Python's legacy SQLite transaction mode does not BEGIN for DDL. Establish
    # an actual transaction before creating the version/table set, including on
    # a standalone migration connection. PostgreSQL already transacts its DDL.
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    if "repositories" not in inspect(connection).get_table_names():
        raise RuntimeError("Initialize/migrate the application database first")
    version_table.create(connection, checkfirst=True)
    version = connection.execute(select(version_table.c.version)).scalar()
    if version not in (None, 1):
        raise RuntimeError("Unsupported PR orchestration schema version")
    for table in TABLES:
        table.create(connection, checkfirst=True)
    if version is None:
        connection.execute(version_table.insert().values(version=1))


def main():
    from dotenv import load_dotenv

    from .database import get_engine

    load_dotenv()
    with get_engine().begin() as connection:
        upgrade(connection)


if __name__ == "__main__":
    main()
