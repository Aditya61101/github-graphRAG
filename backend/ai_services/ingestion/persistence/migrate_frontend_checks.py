"""Additive Stage 4.4 schema; does not rewrite any original evidence."""

from sqlalchemy import Column, Integer, MetaData, Table, select

from ai_services.decision_guard.orchestration.read_models import TABLES

version_table = Table(
    "dg_frontend_checks_schema_version",
    MetaData(),
    Column("version", Integer, nullable=False),
)


def upgrade(connection):
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    version_table.create(connection, checkfirst=True)
    version = connection.execute(select(version_table.c.version)).scalar()
    if version not in (None, 1):
        raise RuntimeError("Unsupported frontend/checks schema version")
    for table in TABLES:
        table.create(connection, checkfirst=True)
    if version is None:
        connection.execute(version_table.insert().values(version=1))


def main():
    from dotenv import load_dotenv

    from .database import get_engine
    from .migrate_pr_orchestration import upgrade as orchestration_upgrade

    load_dotenv()
    with get_engine().begin() as connection:
        orchestration_upgrade(connection)
        upgrade(connection)


if __name__ == "__main__":
    main()
