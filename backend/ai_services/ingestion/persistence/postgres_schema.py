"""Explicit, transactional PostgreSQL schema v3 initialization and validation."""
import argparse
import os
import sys

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError

from .models import Base
from .migrate_ingestion import version_table as ingestion_version, upgrade as upgrade_ingestion, EXTRA_COLUMNS
from .migrate_pr_orchestration import version_table as orchestration_version, upgrade as upgrade_orchestration
from .migrate_frontend_checks import version_table as frontend_version, upgrade as upgrade_frontend
from ai_services.decision_guard.orchestration.models import TABLES as ORCHESTRATION_TABLES
from ai_services.decision_guard.orchestration.read_models import TABLES as FRONTEND_TABLES

SCHEMA = "public"
SCHEMA_VERSION = 3
LOCK_KEY = 4920260302
VERSION_TABLE = Table("app_schema_version", MetaData(), Column("version", Integer, nullable=False), schema=SCHEMA)
COMMAND = "python -m ai_services.ingestion.persistence.postgres_schema"
COMPONENT_TABLES = [*ORCHESTRATION_TABLES, *FRONTEND_TABLES]
VERSION_TABLES = [(VERSION_TABLE, 3), (orchestration_version, 1), (frontend_version, 1), (ingestion_version, 1)]


class PostgreSQLSchemaError(RuntimeError):
    """Safe schema error containing no driver details or credentials."""


def _require_postgresql(engine):
    if engine.dialect.name != "postgresql":
        raise PostgreSQLSchemaError("This schema command supports PostgreSQL only.")


def _prepare(conn, shared=False):
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text("SET LOCAL statement_timeout = '60s'"))
    function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
    if not conn.execute(text(f"SELECT {function}(:key)"), {"key": LOCK_KEY}).scalar():
        raise PostgreSQLSchemaError("PostgreSQL schema initialization is busy; retry after it completes.")
    if conn.execute(text("SELECT current_schema()")).scalar() != SCHEMA:
        raise PostgreSQLSchemaError("PostgreSQL requires public as the current application schema; configure search_path.")


def _tables(conn):
    inspector = inspect(conn)
    if inspector.get_view_names(schema=SCHEMA) or inspector.get_materialized_view_names(schema=SCHEMA):
        raise PostgreSQLSchemaError("Unexpected views in the PostgreSQL application schema; use a dedicated schema.")
    return set(inspector.get_table_names(schema=SCHEMA))


def _validate_table(conn, inspector, table, *, legacy_ingestion=False):
    name = table.name
    actual = {c["name"]: c for c in inspector.get_columns(name, schema=SCHEMA)}
    expected_columns = set(table.columns.keys())
    if legacy_ingestion and name == "ingestion_runs":
        expected_columns -= set(EXTRA_COLUMNS) - set(actual)
    if set(actual) != expected_columns:
        raise PostgreSQLSchemaError(f"PostgreSQL schema is incomplete or incompatible: columns in {name}.")
    for column in table.columns:
        if column.name not in actual and legacy_ingestion:
            continue
        reflected = actual[column.name]
        actual_type = str(reflected["type"].compile(dialect=conn.dialect)).upper()
        expected_type = str(column.type.compile(dialect=conn.dialect)).upper()
        # PostgreSQL stores unqualified FLOAT as DOUBLE PRECISION and reflects
        # that canonical spelling. Do not reject tables created by our own DDL.
        if isinstance(column.type, Float) and column.type.precision is None and actual_type == 'DOUBLE PRECISION':
            actual_type = 'FLOAT'
        if (actual_type != expected_type
                or reflected["nullable"] != column.nullable):
            raise PostgreSQLSchemaError(f"PostgreSQL schema is incompatible: column {name}.{column.name}.")
    if tuple(inspector.get_pk_constraint(name, schema=SCHEMA)["constrained_columns"]) != tuple(c.name for c in table.primary_key):
        raise PostgreSQLSchemaError(f"PostgreSQL schema is incompatible: primary key in {name}.")
    indexes = {i["name"]: i for i in inspector.get_indexes(name, schema=SCHEMA)}
    for index in table.indexes:
        reflected = indexes.get(index.name)
        if (not reflected or tuple(reflected["column_names"]) != tuple(c.name for c in index.columns)
                or bool(reflected["unique"]) != bool(index.unique)):
            raise PostgreSQLSchemaError(f"PostgreSQL schema is incomplete or incompatible: index in {name}.")
    unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name, schema=SCHEMA)}
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and tuple(c.name for c in constraint.columns) not in unique:
            raise PostgreSQLSchemaError(f"PostgreSQL schema is incomplete: unique constraint in {name}.")
    foreign_keys = {
        (tuple(f["constrained_columns"]), f["referred_schema"] or SCHEMA, f["referred_table"],
         tuple(f["referred_columns"]), (f.get("options", {}).get("ondelete") or "NO ACTION").upper())
        for f in inspector.get_foreign_keys(name, schema=SCHEMA)
    }
    for constraint in table.foreign_key_constraints:
        expected = (tuple(c.name for c in constraint.columns), SCHEMA,
                    constraint.elements[0].column.table.name,
                    tuple(e.column.name for e in constraint.elements),
                    (constraint.ondelete or "NO ACTION").upper())
        if expected not in foreign_keys:
            raise PostgreSQLSchemaError(f"PostgreSQL schema is incomplete or incompatible: foreign key in {name}.")


def _validate(conn, *, allow_missing_components=False):
    expected = set(Base.metadata.tables) | {t.name for t, _ in VERSION_TABLES}
    component_names = {t.name for t in COMPONENT_TABLES} | {orchestration_version.name, frontend_version.name,
        ingestion_version.name, "repository_ingestion_state", "ingestion_deliveries"}
    actual = _tables(conn)
    if not actual:
        raise PostgreSQLSchemaError(f"PostgreSQL schema is not initialized; run {COMMAND} explicitly.")
    required = expected - component_names if allow_missing_components else expected
    if not required <= actual or actual - expected:
        raise PostgreSQLSchemaError(f"PostgreSQL schema has missing or unexpected tables; run {COMMAND} --upgrade explicitly for a core v3 database. No automatic repair is performed.")
    inspector = inspect(conn)
    for marker, version in VERSION_TABLES:
        if marker.name not in actual:
            continue
        _validate_table(conn, inspector, marker)
        versions = conn.execute(select(marker.c.version)).scalars().all()
        if versions != [version]:
            raise PostgreSQLSchemaError(f"PostgreSQL requires exactly one {marker.name} row with version {version}.")
    for table in Base.metadata.sorted_tables:
        if table.name in actual:
            _validate_table(conn, inspector, table, legacy_ingestion=allow_missing_components)


def validate_postgresql_schema(engine):
    """Startup validation only: never creates or alters tables."""
    _require_postgresql(engine)
    try:
        with engine.begin() as conn:
            _prepare(conn, shared=True)
            _validate(conn)
    except SQLAlchemyError:
        raise PostgreSQLSchemaError("Unable to validate PostgreSQL schema; check connectivity, privileges, and initialization.") from None


def initialize_postgresql_schema(engine):
    """Create a fresh schema atomically, or validate a complete existing schema."""
    _require_postgresql(engine)
    try:
        with engine.begin() as conn:
            _prepare(conn)
            if _tables(conn):
                _validate(conn)
                return False
            Base.metadata.create_all(conn, checkfirst=False)
            VERSION_TABLE.create(conn, checkfirst=False)
            conn.execute(VERSION_TABLE.insert().values(version=SCHEMA_VERSION))
            upgrade_orchestration(conn)
            upgrade_frontend(conn)
            upgrade_ingestion(conn)
            # New Inspector instances avoid reusing pre-DDL reflection caches.
            _validate(conn)
        return True
    except SQLAlchemyError:
        raise PostgreSQLSchemaError("Unable to initialize PostgreSQL schema; transaction rolled back. Check connectivity and privileges.") from None


def upgrade_postgresql_schema(engine):
    """Explicit additive upgrade of core v3, under the same exclusive DDL lock.

    Validate all existing tables before DDL. Never rewrite core data or version.
    A failed post-migration validation rolls back the entire transaction.
    """
    _require_postgresql(engine)
    try:
        with engine.begin() as conn:
            _prepare(conn)
            _validate(conn, allow_missing_components=True)
            upgrade_orchestration(conn)
            upgrade_frontend(conn)
            upgrade_ingestion(conn)
            _validate(conn)
    except SQLAlchemyError:
        raise PostgreSQLSchemaError("Unable to upgrade PostgreSQL schema; transaction rolled back. Check connectivity and privileges.") from None


def main():
    from dotenv import load_dotenv

    from .database import get_engine
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="Explicitly add Stage 4.3/4.4 tables to core schema v3")
    parser.add_argument("--validate", action="store_true", help="Validate only; never perform DDL")
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL must be explicitly configured for PostgreSQL initialization.", file=sys.stderr)
        return 1
    args = parser.parse_args()
    if args.upgrade and args.validate:
        parser.error("Choose either --upgrade or --validate")
    engine = None
    try:
        engine = get_engine()
        if args.upgrade:
            upgrade_postgresql_schema(engine)
        elif args.validate:
            validate_postgresql_schema(engine)
        else:
            initialize_postgresql_schema(engine)
        print("PostgreSQL core v3 and Stage 4.3/4.4 schemas validated.")
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
