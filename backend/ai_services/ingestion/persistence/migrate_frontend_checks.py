"""Additive Stage 4.4 schema; does not rewrite any original evidence."""

from sqlalchemy import Column, Integer, MetaData, Table, inspect, select, text

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
    upgrade_check_lifecycle(connection)
    if version is None:
        connection.execute(version_table.insert().values(version=1))


def upgrade_check_lifecycle(connection):
    """Additive Phase A; preserve IDs, statuses, conclusions and row versions.

    Existing publications start as legacy completed intent at version zero.
    Enabled recovery derives missing progress intent from live durable jobs.
    No extra marker table/version bump: the compatibility image accepts either
    shape; enabled startup requires all three columns.
    """
    columns = {c['name'] for c in inspect(connection).get_columns('dg_check_publications')}
    for name, definition in (
        ('desired_state', "VARCHAR(16) NOT NULL DEFAULT 'completed'"),
        ('desired_version', 'INTEGER NOT NULL DEFAULT 0'),
        ('published_version', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            connection.execute(text(f'ALTER TABLE dg_check_publications ADD COLUMN {name} {definition}'))


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
