"""Explicit, transactional SQLite upgrade. Run: python -m ai_services.ingestion.persistence.migrate_github_app"""
from __future__ import annotations

import os
from cryptography.fernet import Fernet
from sqlalchemy import inspect, text

from .models import Base

VERSION = 1


def upgrade(engine, encryption_key: str) -> None:
    cipher = Fernet(encryption_key.encode())
    with engine.begin() as conn:
        tables = inspect(conn).get_table_names()
        if 'app_schema_version' in tables:
            current = conn.exec_driver_sql('SELECT version FROM app_schema_version').scalar()
            if current == VERSION:
                return
            raise RuntimeError('Unsupported application database schema version')
        additions = {
            'github_connections': {
                'credential_kind': "VARCHAR(32) NOT NULL DEFAULT 'legacy_oauth'",
                'access_token_expires_at': 'DATETIME',
                'access_status': "VARCHAR(32) NOT NULL DEFAULT 'RECONNECT_REQUIRED'",
            },
            'repositories': {
                'user_id': 'VARCHAR(64) REFERENCES users(id)',
                'installation_id': 'VARCHAR(64) REFERENCES github_installations(id)',
                'access_state': "VARCHAR(32) NOT NULL DEFAULT 'RECONNECT_REQUIRED'",
            },
        }
        for table, fields in additions.items():
            if table not in tables:
                continue
            columns = {column['name'] for column in inspect(conn).get_columns(table)}
            for name, definition in fields.items():
                if name not in columns:
                    conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        Base.metadata.create_all(conn)
        conn.exec_driver_sql('''UPDATE repositories SET user_id =
            (SELECT user_id FROM github_connections WHERE github_connections.id = repositories.github_connection_id)
            WHERE user_id IS NULL AND github_connection_id IS NOT NULL''')
        for row in conn.execute(text('SELECT id, access_token FROM github_connections')):
            token = row.access_token
            if token and not token.startswith('enc:v1:'):
                conn.execute(text('UPDATE github_connections SET access_token=:token WHERE id=:id'),
                             {'id': row.id, 'token': 'enc:v1:' + cipher.encrypt(token.encode()).decode()})
        conn.exec_driver_sql('CREATE TABLE app_schema_version (version INTEGER NOT NULL)')
        conn.exec_driver_sql(f'INSERT INTO app_schema_version VALUES ({VERSION})')


def main():
    from dotenv import load_dotenv
    from .database import get_engine
    load_dotenv()
    key = os.getenv('GITHUB_TOKEN_ENCRYPTION_KEY')
    if not key:
        raise RuntimeError('Set GITHUB_TOKEN_ENCRYPTION_KEY before upgrading; back up SQLite first')
    upgrade(get_engine(), key)
    print('Application database upgraded to GitHub App schema version 1.')


if __name__ == '__main__':
    main()
