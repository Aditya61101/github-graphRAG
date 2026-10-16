"""Explicit, transactional PostgreSQL schema v3 initialization and validation."""
import os
import sys

from sqlalchemy import (
    Column,
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

SCHEMA = "public"
SCHEMA_VERSION = 3
LOCK_KEY = 4920260302
VERSION_TABLE = Table("app_schema_version", MetaData(), Column("version", Integer, nullable=False), schema=SCHEMA)
COMMAND = "python -m ai_services.ingestion.persistence.postgres_schema"


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


def _validate_table(conn, inspector, table):
    name = table.name
    actual = {c["name"]: c for c in inspector.get_columns(name, schema=SCHEMA)}
    if set(actual) != set(table.columns.keys()):
        raise PostgreSQLSchemaError(f"PostgreSQL schema is incomplete or incompatible: columns in {name}.")
    for column in table.columns:
        reflected = actual[column.name]
        if (str(reflected["type"].compile(dialect=conn.dialect)).upper() !=
                str(column.type.compile(dialect=conn.dialect)).upper()
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


def _validate(conn):
    expected = set(Base.metadata.tables) | {VERSION_TABLE.name}
    actual = _tables(conn)
    if not actual:
        raise PostgreSQLSchemaError(f"PostgreSQL schema is not initialized; run {COMMAND} explicitly.")
    if actual != expected:
        raise PostgreSQLSchemaError("PostgreSQL schema has missing or unexpected tables; no automatic repair is performed.")
    inspector = inspect(conn)
    _validate_table(conn, inspector, VERSION_TABLE)
    versions = conn.execute(select(VERSION_TABLE.c.version)).scalars().all()
    if versions != [SCHEMA_VERSION]:
        raise PostgreSQLSchemaError("PostgreSQL requires exactly one app_schema_version row with version 3.")
    for table in Base.metadata.sorted_tables:
        _validate_table(conn, inspector, table)


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
            # New Inspector instances avoid reusing pre-DDL reflection caches.
            _validate(conn)
        return True
    except SQLAlchemyError:
        raise PostgreSQLSchemaError("Unable to initialize PostgreSQL schema; transaction rolled back. Check connectivity and privileges.") from None


def main():
    from dotenv import load_dotenv

    from .database import get_engine
    load_dotenv()
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL must be explicitly configured for PostgreSQL initialization.", file=sys.stderr)
        return 1
    engine = None
    try:
        engine = get_engine()
        created = initialize_postgresql_schema(engine)
        print("PostgreSQL schema v3 initialized." if created else "PostgreSQL schema v3 already initialized and validated.")
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
