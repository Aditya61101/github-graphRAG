"""Explicit SQLite v1 -> v2 upgrade: python -m ai_services.ingestion.persistence.migrate_github_refresh."""
from sqlalchemy import inspect


def upgrade(engine):
    with engine.begin() as conn:
        if 'app_schema_version' not in inspect(conn).get_table_names():
            raise RuntimeError('Run migrate_github_app first')
        version = conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar()
        if version == 2:
            return
        if version != 1:
            raise RuntimeError('Unsupported application database schema version')
        columns = {column['name'] for column in inspect(conn).get_columns('github_connections')}
        for name, definition in [('refresh_token', 'TEXT'), ('refresh_token_expires_at', 'DATETIME')]:
            if name not in columns:
                conn.exec_driver_sql(f'ALTER TABLE github_connections ADD COLUMN {name} {definition}')
        conn.exec_driver_sql('UPDATE app_schema_version SET version = 2')


if __name__ == '__main__':
    from dotenv import load_dotenv
    from .database import get_engine
    load_dotenv()
    upgrade(get_engine())
    print('Application database upgraded to GitHub refresh schema version 2.')
