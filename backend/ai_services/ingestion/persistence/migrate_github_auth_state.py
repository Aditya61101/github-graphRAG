"""Add shared ephemeral authentication state; no credential/onboarding backfill."""
from sqlalchemy import Column, Integer, MetaData, Table
from .models import GitHubAuthState

version_table = Table('github_auth_schema_version', MetaData(), Column('version', Integer, nullable=False))


def upgrade(conn):
    GitHubAuthState.__table__.create(conn, checkfirst=True)
    version_table.create(conn, checkfirst=True)
    versions = conn.execute(version_table.select()).scalars().all()
    if not versions:
        conn.execute(version_table.insert().values(version=1))
    elif versions != [1]:
        raise RuntimeError('Unsupported GitHub authentication schema version')
