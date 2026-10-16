from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Integer, create_engine, create_mock_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from ai_services.ingestion.persistence import database
from ai_services.ingestion.persistence import postgres_schema as schema
from ai_services.ingestion.persistence.models import Base


@pytest.fixture
def pg(monkeypatch):
    tables = {t.name: t for t in [*Base.metadata.tables.values(), schema.VERSION_TABLE]}
    state = SimpleNamespace(tables=set(tables), versions=[3], committed=False, rolled_back=False)
    inspector = MagicMock()
    inspector.get_table_names.side_effect = lambda **kw: list(state.tables)
    inspector.get_view_names.return_value = []
    inspector.get_materialized_view_names.return_value = []
    inspector.get_columns.side_effect = lambda name, **kw: [
        {"name": c.name, "type": c.type, "nullable": c.nullable} for c in tables[name].columns]
    inspector.get_pk_constraint.side_effect = lambda name, **kw: {
        "constrained_columns": [c.name for c in tables[name].primary_key]}
    inspector.get_indexes.side_effect = lambda name, **kw: [
        {"name": i.name, "column_names": [c.name for c in i.columns], "unique": i.unique}
        for i in tables[name].indexes]
    inspector.get_unique_constraints.return_value = []
    inspector.get_foreign_keys.side_effect = lambda name, **kw: [
        {"constrained_columns": [c.name for c in f.columns], "referred_schema": None,
         "referred_table": f.elements[0].column.table.name,
         "referred_columns": [e.column.name for e in f.elements], "options": {"ondelete": f.ondelete}}
        for f in tables[name].foreign_key_constraints]
    monkeypatch.setattr(schema, "inspect", lambda conn: inspector)
    conn = MagicMock()
    conn.dialect = postgresql.dialect()
    def execute(statement, *args):
        result = MagicMock()
        sql = str(statement)
        if "pg_try_advisory" in sql:
            result.scalar.return_value = True
        elif "current_schema" in sql:
            result.scalar.return_value = "public"
        elif sql.startswith("SELECT public.app_schema_version"):
            result.scalars.return_value.all.return_value = state.versions
        elif sql.startswith("INSERT INTO public.app_schema_version"):
            state.versions = [3]
        return result
    conn.execute.side_effect = execute
    engine = SimpleNamespace(dialect=conn.dialect)
    @contextmanager
    def begin():
        original_tables = set(state.tables)
        original_versions = list(state.versions)
        try:
            yield conn
            state.committed = True
        except Exception:
            state.tables = original_tables
            state.versions = original_versions
            state.rolled_back = True
            raise
    engine.begin = begin
    create = MagicMock(side_effect=lambda *args, **kw: state.tables.update(Base.metadata.tables))
    marker = MagicMock(side_effect=lambda *args, **kw: state.tables.add("app_schema_version"))
    monkeypatch.setattr(Base.metadata, "create_all", create)
    monkeypatch.setattr(schema.VERSION_TABLE, "create", marker)
    return SimpleNamespace(engine=engine, conn=conn, inspector=inspector, state=state, create=create, marker=marker)


def test_fresh_initialization_and_idempotent_rerun(pg):
    pg.state.tables = set()
    pg.state.versions = []
    assert schema.initialize_postgresql_schema(pg.engine)
    assert pg.state.committed
    assert pg.state.tables == set(Base.metadata.tables) | {"app_schema_version"}
    assert pg.state.versions == [3]
    pg.create.assert_called_once_with(pg.conn, checkfirst=False)
    pg.marker.assert_called_once_with(pg.conn, checkfirst=False)
    statements = [str(c.args[0]) for c in pg.conn.execute.call_args_list]
    assert next(i for i, s in enumerate(statements) if "pg_try_advisory_xact_lock(" in s) < next(i for i, s in enumerate(statements) if "INSERT INTO" in s)
    assert not schema.initialize_postgresql_schema(pg.engine)
    pg.create.assert_called_once()
    pg.marker.assert_called_once()


def test_startup_validation_never_creates_tables(pg, monkeypatch):
    monkeypatch.setattr(database, "get_engine", lambda: pg.engine)
    database.init_db()
    pg.create.assert_not_called()
    pg.marker.assert_not_called()
    statements = [str(c.args[0]) for c in pg.conn.execute.call_args_list]
    assert any("pg_try_advisory_xact_lock_shared" in s for s in statements)
    assert all(not s.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE")) for s in statements)


def test_startup_missing_schema_fails(pg, monkeypatch):
    pg.state.tables = set()
    monkeypatch.setattr(database, "get_engine", lambda: pg.engine)
    with pytest.raises(schema.PostgreSQLSchemaError, match="not initialized"):
        database.init_db()
    pg.create.assert_not_called()


@pytest.mark.parametrize("versions", [[], [1], [2], [4], [3, 3], [None]])
@pytest.mark.parametrize("operation", [schema.initialize_postgresql_schema, schema.validate_postgresql_schema])
def test_unsupported_or_invalid_marker_is_rejected_without_changes(pg, versions, operation):
    pg.state.versions = versions
    with pytest.raises(schema.PostgreSQLSchemaError, match="exactly one"):
        operation(pg.engine)
    assert pg.state.rolled_back
    pg.create.assert_not_called()


@pytest.mark.parametrize("change", ["missing_table", "extra_table", "column", "index", "foreign_key", "primary_key", "type", "view"])
@pytest.mark.parametrize("operation", [schema.initialize_postgresql_schema, schema.validate_postgresql_schema])
def test_incomplete_schema_is_rejected(pg, change, operation):
    if change == "missing_table":
        pg.state.tables.remove("adrs")
    elif change == "extra_table":
        pg.state.tables.add("unrelated")
    elif change == "column":
        original = pg.inspector.get_columns.side_effect
        pg.inspector.get_columns.side_effect = lambda name, **kw: original(name, **kw)[1:]
    elif change == "index":
        pg.inspector.get_indexes.side_effect = None
        pg.inspector.get_indexes.return_value = []
    elif change == "foreign_key":
        pg.inspector.get_foreign_keys.side_effect = None
        pg.inspector.get_foreign_keys.return_value = []
    elif change == "primary_key":
        pg.inspector.get_pk_constraint.side_effect = None
        pg.inspector.get_pk_constraint.return_value = {"constrained_columns": []}
    elif change == "type":
        original = pg.inspector.get_columns.side_effect
        def changed(name, **kw):
            columns = original(name, **kw)
            if name == "users":
                columns[0]["type"] = Integer()
            return columns
        pg.inspector.get_columns.side_effect = changed
    elif change == "view":
        pg.inspector.get_view_names.return_value = ["unexpected"]
    with pytest.raises(schema.PostgreSQLSchemaError):
        operation(pg.engine)
    pg.create.assert_not_called()
    pg.marker.assert_not_called()


def test_post_ddl_validation_failure_rolls_back(pg):
    pg.state.tables = set()
    pg.state.versions = []
    pg.marker.side_effect = lambda *args, **kw: None
    with pytest.raises(schema.PostgreSQLSchemaError, match="missing or unexpected"):
        schema.initialize_postgresql_schema(pg.engine)
    assert pg.state.rolled_back
    assert pg.state.tables == set() and pg.state.versions == []


def test_busy_lock_prevents_creation(pg):
    pg.state.tables = set()
    pg.conn.execute.side_effect = lambda *args: SimpleNamespace(scalar=lambda: False)
    with pytest.raises(schema.PostgreSQLSchemaError, match="busy"):
        schema.initialize_postgresql_schema(pg.engine)
    pg.create.assert_not_called()
    pg.inspector.get_table_names.assert_not_called()


def test_driver_error_is_sanitized(pg):
    pg.conn.execute.side_effect = OperationalError("secret-url", {}, Exception("private-password"))
    with pytest.raises(schema.PostgreSQLSchemaError) as error:
        schema.initialize_postgresql_schema(pg.engine)
    assert "secret-url" not in str(error.value) and "private-password" not in str(error.value)
    assert error.value.__suppress_context__
    assert pg.state.rolled_back


def test_sqlite_command_rejected_before_connecting():
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    with pytest.raises(schema.PostgreSQLSchemaError, match="PostgreSQL only"):
        schema.initialize_postgresql_schema(engine)
    engine.begin.assert_not_called()


def test_metadata_compiles_all_postgresql_tables_indexes_and_constraints():
    statements = []
    engine = create_mock_engine("postgresql+psycopg://", lambda ddl, *args, **kw: statements.append(str(ddl.compile(dialect=postgresql.dialect()))))
    Base.metadata.create_all(engine, checkfirst=False)
    schema.VERSION_TABLE.create(engine, checkfirst=False)
    assert len([s for s in statements if "CREATE TABLE" in s]) == 10
    assert any("FOREIGN KEY" in s for s in statements)
    assert any("CREATE UNIQUE INDEX" in s for s in statements)
    assert "app_schema_version" in statements[-1]


def test_sqlite_fresh_and_rerun_preserve_data(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    try:
        database.init_db()
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, username, created_at, updated_at) VALUES ('user', 'name', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        database.init_db()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version FROM app_schema_version")).scalar() == 3
            assert conn.execute(text("SELECT id FROM users")).scalar() == "user"
            assert set(inspect(conn).get_table_names()) == set(Base.metadata.tables) | {"app_schema_version"}
    finally:
        engine.dispose()


def test_wrong_current_schema_rejected(pg):
    original = pg.conn.execute.side_effect
    def execute(statement, *args):
        if "current_schema" in str(statement):
            return SimpleNamespace(scalar=lambda: "user_schema")
        return original(statement, *args)
    pg.conn.execute.side_effect = execute
    with pytest.raises(schema.PostgreSQLSchemaError, match="search_path"):
        schema.initialize_postgresql_schema(pg.engine)
    pg.create.assert_not_called()


def test_cli_requires_explicit_database_url(monkeypatch, capsys):
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    factory = MagicMock()
    monkeypatch.setattr(database, "get_engine", factory)
    assert schema.main() == 1
    factory.assert_not_called()
    assert "DATABASE_URL must be explicitly configured" in capsys.readouterr().err
