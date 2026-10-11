"""Explicit SQLite v2 -> v3 upgrade. Back up SQLite before running this module."""
from sqlalchemy import inspect


def upgrade(engine):
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('This migration supports SQLite only')
    with engine.connect() as conn:
        # Explicit BEGIN makes SQLite DDL transactional and excludes concurrent
        # user creation until the legacy-user backfill and version update commit.
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            tables = inspect(conn).get_table_names()
            if 'app_schema_version' not in tables or 'users' not in tables:
                raise RuntimeError('Run the existing GitHub App and refresh migrations first')
            versions = conn.exec_driver_sql('SELECT version FROM app_schema_version').scalars().all()
            if versions not in ([2], [3]):
                raise RuntimeError('Unsupported application database schema version')
            columns = {c['name'] for c in inspect(conn).get_columns('users')}
            if versions == [3]:
                if 'onboarding_completed_at' not in columns:
                    raise RuntimeError('Invalid schema v3: users.onboarding_completed_at is missing')
                conn.commit()
                return
            if 'onboarding_completed_at' not in columns:
                conn.exec_driver_sql('ALTER TABLE users ADD COLUMN onboarding_completed_at DATETIME')
            # No column default: users created after this migration remain NULL.
            # Preserve any completion timestamps already present in a v2 database.
            conn.exec_driver_sql('UPDATE users SET onboarding_completed_at = CURRENT_TIMESTAMP '
                                 'WHERE onboarding_completed_at IS NULL')
            conn.exec_driver_sql('UPDATE app_schema_version SET version = 3')
            conn.commit()
        except Exception:
            conn.rollback()
            raise


if __name__ == '__main__':
    from dotenv import load_dotenv
    from .database import get_engine
    load_dotenv()
    upgrade(get_engine())
    print('Application database upgraded to onboarding schema version 3.')
