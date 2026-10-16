from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ai_services.github_app.service import GitHubAppError, GitHubAppService
from ai_services.ingestion.persistence.concurrency import (
    METADATA_WRITE_LOCK,
    serialize_metadata_write,
)
from ai_services.ingestion.persistence.models import (
    ADRModel,
    Base,
    GitHubConnectionModel,
    UserModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.sources.interface import (
    RepositoryMetadata,
    RepositoryOwnershipConflictError,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    engine = create_engine(f'sqlite:///{tmp_path / "concurrency.db"}', connect_args={'check_same_thread': False, 'timeout': 10})
    @event.listens_for(engine, 'connect')
    def foreign_keys(dbapi_conn, record):
        dbapi_conn.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    result = SqliteApplicationStore(sessionmaker(bind=engine, expire_on_commit=False))
    result.upsert_user('owner', user_id='owner')
    result.upsert_user('other', user_id='other')
    result.save_repository(metadata(), user_id='owner')
    yield result
    engine.dispose()


def metadata():
    return RepositoryMetadata(github_repository_id='101', owner='owner', name='repo', full_name='owner/repo',
        default_branch='main', tracked_branch='main', repository_url='https://github.com/owner/repo', visibility='public')


def parallel(call):
    barrier = Barrier(2)
    def worker():
        barrier.wait(timeout=10)
        return call()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return [future.result(timeout=20) for future in futures]


def revision():
    return SimpleNamespace(revision_key='revision', github_repository_id=101, pull_request_number=1,
        base_sha='a' * 40, head_sha='b' * 40, delivery_id='delivery')


def test_duplicate_pr_claim_retry_and_terminal_completion(store):
    event = revision()
    assert sorted(parallel(lambda: store.claim_pr_revision('repo_101', event))) == [False, True]
    store.finish_pr_revision(event.revision_key, 'FAILED')
    assert sorted(parallel(lambda: store.claim_pr_revision('repo_101', event))) == [False, True]
    assert store.get_pr_revision(event.revision_key).attempts == 2
    store.finish_pr_revision(event.revision_key, 'COMPLETED')
    store.finish_pr_revision(event.revision_key, 'FAILED', 'late failure')
    assert store.get_pr_revision(event.revision_key).status == 'COMPLETED'


def test_sqlite_startup_recovery_and_targeted_manual_recovery(store):
    event = revision()
    store.claim_pr_revision('repo_101', event)
    with pytest.raises(ValueError, match='worker'):
        store.recover_pr_revision(event.revision_key, 1)
    assert not store.recover_pr_revision(event.revision_key, 2, worker_stopped=True)
    assert store.get_pr_revision(event.revision_key).status == 'RUNNING'
    assert store.recover_pr_revision(event.revision_key, 1, worker_stopped=True)
    assert not store.recover_pr_revision(event.revision_key, 1, worker_stopped=True)
    assert store.claim_pr_revision('repo_101', event)
    store.recover_interrupted_pr_revisions()
    assert store.get_pr_revision(event.revision_key).error_type == 'ProcessInterrupted'


def test_postgresql_startup_recovery_issues_no_updates():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = 'postgresql'
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    store = SqliteApplicationStore(factory)
    store.recover_interrupted_pr_revisions()
    session.execute.assert_not_called()
    session.commit.assert_not_called()


def test_postgresql_metadata_lock_is_transaction_scoped_and_bounded():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = 'postgresql'
    serialize_metadata_write(session)
    assert str(session.execute.call_args_list[0].args[0]) == "SET LOCAL lock_timeout = '10s'"
    assert 'pg_advisory_xact_lock' in str(session.execute.call_args_list[1].args[0])
    assert session.execute.call_args_list[1].args[1] == {'key': METADATA_WRITE_LOCK}
    session.commit.assert_not_called()


def test_concurrent_user_and_oauth_inserts(store):
    users = parallel(lambda: store.upsert_user('same'))
    assert users[0].id == users[1].id
    results = parallel(lambda: store.upsert_github_login(github_user_id='1000', username='new-user',
        access_token='token', refresh_token='refresh'))
    assert results[0][0].id == results[1][0].id
    assert results[0][1].id == results[1][1].id
    with store.session_factory() as session:
        assert len(session.scalars(select(GitHubConnectionModel)).all()) == 1
        assert len(session.scalars(select(UserModel).where(UserModel.username == 'new-user')).all()) == 1


def test_concurrent_repository_creation_and_ownership_conflict(store):
    result = parallel(lambda: store.save_repository(metadata(), user_id='owner'))
    assert result[0].id == result[1].id
    with pytest.raises(RepositoryOwnershipConflictError):
        store.save_repository(metadata(), user_id='other')
    assert store.get_repository('repo_101').user_id == 'owner'


def test_concurrent_conversation_binding_preserves_scope(store):
    service = GitHubAppService(store, MagicMock(), MagicMock())
    parallel(lambda: service.bind_conversation('owner', 'repo_101', 'thread'))
    alternate = replace(metadata(), github_repository_id='102', name='alternate', full_name='owner/alternate')
    store.save_repository(alternate, user_id='owner')
    with pytest.raises(GitHubAppError) as error:
        service.bind_conversation('owner', 'repo_102', 'thread')
    assert error.value.status_code == 409


def test_concurrent_adr_claim_and_failed_retry(store):
    claim = lambda: store.claim_or_create_adr('repo_101', 'hash', 'title', 'doc.md', '/tmp/doc.md')
    results = parallel(claim)
    assert results[0][0].id == results[1][0].id
    assert sorted(duplicate for _, duplicate in results) == [False, True]
    with store.session_factory() as session:
        assert len(session.scalars(select(ADRModel)).all()) == 1
    store.update_adr_status(results[0][0].id, 'FAILED')
    results = parallel(claim)
    assert sorted(duplicate for _, duplicate in results) == [False, True]


def test_integrity_error_rolls_back_and_releases_write_lock(store):
    with pytest.raises(IntegrityError):
        store.upsert_github_connection('missing-user', '999', 'token')
    with store.session_factory() as session:
        assert session.scalars(select(GitHubConnectionModel)).first() is None
    assert store.upsert_user('after-rollback').username == 'after-rollback'


def test_concurrent_installation_inventory_and_repository_binding(store):
    service = GitHubAppService(store, SimpleNamespace(app_id='1'), MagicMock())
    installation = {'id': 10, 'account': {'id': 20, 'login': 'owner', 'type': 'User'}, 'repository_selection': 'selected'}
    repositories = [{'id': 101, 'full_name': 'owner/repo', 'private': False}]
    parallel(lambda: service.persist_inventory(installation, repositories, 'ACTIVE'))
    store.upsert_github_connection('owner', '100', 'token')
    item = {'id': 101, 'owner': {'login': 'owner'}, 'name': 'repo', 'full_name': 'owner/repo',
        'html_url': 'https://github.com/owner/repo', 'installation_id': '10'}
    results = parallel(lambda: service.bind_repository(item, 'owner'))
    assert all(row.installation_id == '10' and row.user_id == 'owner' for row in results)
    from ai_services.ingestion.persistence.models import (
        GitHubInstallationModel,
        InstallationRepositoryModel,
    )
    with store.session_factory() as session:
        assert len(session.scalars(select(GitHubInstallationModel)).all()) == 1
        assert len(session.scalars(select(InstallationRepositoryModel)).all()) == 1


def test_stale_oauth_rotation_cannot_overwrite_new_credentials(store):
    _, previous = store.upsert_github_login('700', 'login', 'first', refresh_token='refresh-first')
    store.rotate_github_tokens(previous, 'second', 'refresh-second', None, None)
    with pytest.raises(RuntimeError, match='authorization changed'):
        store.rotate_github_tokens(previous, 'stale', 'refresh-stale', None, None)
    current = store.get_user_github_connection(previous.user_id)
    assert store.decrypt_token(current.access_token) == 'second'
    assert store.decrypt_token(current.refresh_token) == 'refresh-second'


def test_binding_cannot_reactivate_removed_inventory(store):
    service = GitHubAppService(store, SimpleNamespace(app_id='1'), MagicMock())
    installation = {'id': 10, 'account': {'id': 20, 'login': 'owner', 'type': 'User'}, 'repository_selection': 'selected'}
    service.persist_inventory(installation, [], 'ACTIVE')
    store.upsert_github_connection('owner', '100', 'token')
    stale_item = {'id': 101, 'owner': {'login': 'owner'}, 'name': 'repo', 'full_name': 'owner/repo',
        'html_url': 'https://github.com/owner/repo', 'installation_id': '10'}
    with pytest.raises(GitHubAppError, match='access changed'):
        service.bind_repository(stale_item, 'owner')
    assert store.get_repository('repo_101').access_state != 'ACTIVE'
