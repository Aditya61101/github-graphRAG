from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_services.ingestion.persistence.models import Base, RepositoryModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.pr.git_reader import PullRequestGitReader
from ai_services.ingestion.pr.models import PullRequestEvent, PullRequestIngestionError, SupersededRevisionError
from ai_services.ingestion.pr.service import PullRequestIngestionService
from ai_services.ingestion.pr.settings import PullRequestSettings
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.ingestion.sources.github import GitHubRepositorySource
from api_services.app.routers.webhook import router


def git(root, *args, input=None):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, input=input)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result.stdout.decode().strip()


def commit(root, message):
    git(root, 'add', '-A')
    git(root, 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def repository(tmp_path):
    source = GitHubRepositorySource(tmp_path / 'repos')
    root = source.storage_dir / 'owner' / 'repo'
    root.mkdir(parents=True)
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.email', 'test@example.com')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'core.autocrlf', 'false')
    git(root, 'config', 'core.protectNTFS', 'false')
    (root / 'service.py').write_bytes(b'first\nsecond\n')
    (root / 'delete.py').write_bytes(b'delete this\n')
    (root / 'old.py').write_text(''.join(f'rename line {i}\n' for i in range(20)))
    (root / 'copy_source.py').write_text(''.join(f'copy line {i}\n' for i in range(20)))
    base = commit(root, 'base')
    (root / 'accepted_only.py').write_text('accepted target advancement\n')
    target = commit(root, 'target advanced')
    git(root, 'checkout', '-b', 'feature', base)
    (root / 'service.py').write_bytes(b'first\nsecond\nthird\n')
    (root / 'added.py').write_bytes(b'new file\n')
    (root / 'delete.py').unlink()
    git(root, 'mv', 'old.py', 'new.py')
    (root / 'copied.py').write_text((root / 'copy_source.py').read_text())
    (root / 'image.bin').write_bytes(b'\x00\xff\x01binary')
    (root / 'large.py').write_text('x' * 2000)
    head = commit(root, 'proposed')
    git(root, 'checkout', 'main')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', str(remote))
    git(root, 'push', remote.as_uri(), 'main:refs/heads/main', 'feature:refs/pull/42/head')
    git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    event = PullRequestEvent(installation_id=10, github_repository_id=101,
        repository_full_name='owner/repo', repository_private=False, pull_request_number=42,
        title='Proposed changes', body='PR description', base_branch='main', head_branch='feature',
        base_sha=target, head_sha=head, base_repository_id=101, head_repository_id=202,
        head_repository_full_name='fork/repo', source_event='opened', delivery_id='delivery-1')
    return SimpleNamespace(source=source, root=root, remote=remote, base=base, target=target,
        head=head, event=event, settings=PullRequestSettings(max_file_bytes=1024), credential=GitHubCredential('test-token'))


@pytest.fixture
def store(repository):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = SqliteApplicationStore(sessionmaker(bind=engine, expire_on_commit=False))
    with store.session_factory() as session:
        session.add(RepositoryModel(id='repo_101', github_repository_id='101', owner='owner', name='repo',
            full_name='owner/repo', repository_url='https://github.com/owner/repo',
            default_branch='main', tracked_branch='main', installation_id='10',
            indexed_commit_sha=repository.target, status='COMPLETED', access_state='ACTIVE'))
        session.commit()
    yield store
    engine.dispose()


def service(repository, store, **kwargs):
    locks = {}
    app = SimpleNamespace(indexing_credential=AsyncMock(return_value=repository.credential))
    instance = PullRequestIngestionService(repository.source, store, app,
        lambda key: locks.setdefault(key, asyncio.Lock()), settings=repository.settings,
        state_dir=repository.root.parent / '.state', **kwargs)
    return instance


def test_object_diff_contents_stats_rename_copy_binary_large_and_worktree_isolation(repository):
    r = repository
    reader = PullRequestGitReader(r.source, r.settings)
    original = r.source._run_git
    r.source._run_git = Mock(wraps=original)
    merge_base, files = reader.read_revision(r.root, r.event, r.credential, r.remote.as_uri())
    assert merge_base == r.base != r.target
    files = {item.path: item for item in files}
    assert 'accepted_only.py' not in files
    modified = files['service.py']
    assert modified.base_content == 'first\nsecond\n'
    assert modified.head_content == 'first\nsecond\nthird\n'
    assert (modified.additions, modified.deletions, modified.changes) == (1, 0, 1)
    assert '+third' in modified.patch
    assert files['added.py'].base_content_status == 'absent'
    assert files['added.py'].head_content == 'new file\n'
    assert files['delete.py'].head_content_status == 'absent'
    assert files['delete.py'].base_content == 'delete this\n'
    assert files['new.py'].status == 'renamed'
    assert files['new.py'].previous_path == 'old.py'
    assert files['new.py'].base_content == files['new.py'].head_content
    assert files['copied.py'].status == 'copied'
    assert files['copied.py'].previous_path == 'copy_source.py'
    assert files['image.bin'].head_content_status == 'binary'
    assert files['image.bin'].changes is None and files['image.bin'].patch is None
    assert files['large.py'].head_content_status == 'oversized'
    assert files['large.py'].head_content is None
    assert git(r.root, 'rev-parse', 'HEAD') == r.target
    assert git(r.root, 'symbolic-ref', '--short', 'HEAD') == 'main'
    assert git(r.root, 'status', '--porcelain') == ''
    assert all('checkout' not in call.args[0] and 'reset' not in call.args[0] for call in r.source._run_git.call_args_list)


def test_deletions_and_empty_diff(repository):
    reader = PullRequestGitReader(repository.source, repository.settings)
    files = {f.path: f for f in reader.build_file_changes(repository.root, repository.base, repository.head)}
    assert (files['delete.py'].additions, files['delete.py'].deletions) == (0, 1)
    assert reader.build_file_changes(repository.root, repository.base, repository.base) == []


@pytest.mark.parametrize('before,after,base_status,head_status', [
    (b'\xff\xfe' + 'alpha==1\n'.encode('utf-16-le'), b'alpha==2\n', 'binary', 'complete'),
    (b'alpha==1\n', b'\xff\xfe' + 'alpha==2\n'.encode('utf-16-le'), 'complete', 'binary'),
    (b'\0\xffold', b'\0\xffnew', 'binary', 'binary'),
    (b'alpha==1\n', b'alpha==2\n', 'complete', 'complete'),
])
def test_content_classification_is_independent_for_each_version(repository, before, after, base_status, head_status):
    r = repository
    path = r.root / 'backend' / 'requirements.txt'
    path.parent.mkdir()
    path.write_bytes(before)
    base = commit(r.root, 'original encoding')
    path.write_bytes(after)
    head = commit(r.root, 'proposed encoding')
    files = PullRequestGitReader(r.source, r.settings).build_file_changes(r.root, base, head)
    assert len(files) == 1
    changed = files[0]
    assert changed.path == 'backend/requirements.txt' and changed.status == 'modified'
    assert changed.base_content_status == base_status
    assert changed.head_content_status == head_status
    assert changed.base_content == (before.decode('utf-8') if base_status == 'complete' else None)
    assert changed.head_content == (after.decode('utf-8') if head_status == 'complete' else None)
    assert changed.base_size_bytes == len(before) and changed.head_size_bytes == len(after)
    if 'binary' in {base_status, head_status}:
        assert changed.additions is None and changed.deletions is None and changed.changes is None
        assert changed.patch is None and changed.patch_status == 'binary'
    else:
        assert (changed.additions, changed.deletions, changed.changes) == (1, 1, 2)
        assert changed.patch_status == 'complete' and '+alpha==2' in changed.patch


def test_content_classification_is_independent_of_git_binary_attributes(repository):
    r = repository
    (r.root / '.gitattributes').write_bytes(b'service.py -diff\n')
    base = commit(r.root, 'force binary diff for text file')
    (r.root / 'service.py').write_bytes(b'first\nsecond\nthird\n')
    head = commit(r.root, 'edit text with binary diff policy')
    changed = PullRequestGitReader(r.source, r.settings).build_file_changes(r.root, base, head)[0]
    assert changed.base_content_status == changed.head_content_status == 'complete'
    assert changed.base_content == 'first\nsecond\n'
    assert changed.head_content == 'first\nsecond\nthird\n'
    assert changed.additions is None and changed.deletions is None and changed.changes is None
    assert changed.patch is None and changed.patch_status == 'binary'


def test_copy_patch_excludes_modified_source(repository):
    r = repository
    git(r.root, 'checkout', '-b', 'copy-edit', r.base)
    original = (r.root / 'copy_source.py').read_bytes()
    (r.root / 'copy_source.py').write_bytes(original + b'source-only change\n')
    (r.root / 'copy-edit.py').write_bytes(original + b'copy-only change\n')
    head = commit(r.root, 'edit source and copy')
    files = {f.path: f for f in PullRequestGitReader(r.source, r.settings).build_file_changes(r.root, r.base, head)}
    copied = files['copy-edit.py']
    assert copied.status == 'copied'
    assert '+copy-only change' in copied.patch
    assert 'source-only change' not in copied.patch


def test_content_failure_is_not_misrepresented_as_absent(repository):
    reader = PullRequestGitReader(repository.source, repository.settings)
    with pytest.raises(Exception):
        reader.get_file_content(repository.root, 'a' * 40, '100644', 1024)


def test_changed_file_limit_fails_without_partial_change_set(repository):
    reader = PullRequestGitReader(repository.source, replace(repository.settings, max_changed_files=1))
    with pytest.raises(PullRequestIngestionError, match='changed-file limit'):
        reader.build_file_changes(repository.root, repository.base, repository.head)


def test_bounded_patch_and_total_content_budget(repository):
    reader = PullRequestGitReader(repository.source, replace(repository.settings, max_patch_bytes=1))
    files = reader.build_file_changes(repository.root, repository.base, repository.head)
    assert any(f.patch_status == 'oversized' for f in files)
    reader = PullRequestGitReader(repository.source, replace(repository.settings, max_total_text_bytes=5))
    files = reader.build_file_changes(repository.root, repository.base, repository.head)
    assert any(f.head_content_status == 'budget_exceeded' for f in files)
    assert all(f.head_content is None or len(f.head_content.encode()) <= 5 for f in files)


def fresh_clone(repository, tmp_path):
    root = tmp_path / 'fresh'
    git(tmp_path, 'clone', '--no-local', '--single-branch', '--branch', 'main', repository.remote.as_uri(), str(root))
    return root


def test_fork_head_ref_fetch_and_no_tracked_ref_change(repository, tmp_path):
    r = repository
    root = fresh_clone(r, tmp_path)
    before = git(root, 'show-ref', '--heads')
    reader = PullRequestGitReader(r.source, r.settings)
    assert not reader._has_commit(root, r.head)
    base, files = reader.read_revision(root, r.event, r.credential, r.remote.as_uri())
    assert base == r.base and files
    assert git(root, 'show-ref', '--heads') == before
    assert git(root, 'rev-parse', 'HEAD') == r.target


def test_obsolete_head_never_substituted_and_locally_available_old_revision_used(repository, tmp_path):
    r = repository
    root = fresh_clone(r, tmp_path)
    git(r.root, 'checkout', '-B', 'replacement', r.base)
    (r.root / 'replacement.py').write_text('different proposed content\n')
    new_head = commit(r.root, 'replace old PR head')
    git(r.root, 'push', '--force', r.remote.as_uri(), 'replacement:refs/pull/42/head')
    git(r.root, 'checkout', 'main')
    reader = PullRequestGitReader(r.source, r.settings)
    with pytest.raises(SupersededRevisionError):
        reader.read_revision(root, r.event, r.credential, r.remote.as_uri())
    _, files = reader.read_revision(r.root, r.event, r.credential, r.remote.as_uri())
    assert any(f.path == 'service.py' for f in files)
    assert not any(f.path == 'replacement.py' for f in files)
    newer = r.event.model_copy(update={'head_sha': new_head})
    assert newer.revision_key != r.event.revision_key


def test_fetch_failure_is_retryable_failure_not_superseded(repository, tmp_path):
    root = fresh_clone(repository, tmp_path)
    with pytest.raises(Exception) as error:
        PullRequestGitReader(repository.source, repository.settings).read_revision(
            root, repository.event, repository.credential, (tmp_path / 'missing.git').as_uri())
    assert not isinstance(error.value, SupersededRevisionError)


def test_missing_base_and_shallow_history_fail_clearly(repository, tmp_path):
    reader = PullRequestGitReader(repository.source, repository.settings)
    missing = repository.event.model_copy(update={'base_sha': 'a' * 40})
    with pytest.raises(Exception):
        reader.read_revision(repository.root, missing, repository.credential, repository.remote.as_uri())
    root = tmp_path / 'shallow'
    git(tmp_path, 'clone', '--depth=1', '--branch', 'main', repository.remote.as_uri(), str(root))
    with pytest.raises(PullRequestIngestionError, match='merge base'):
        reader.read_revision(root, repository.event, repository.credential, repository.remote.as_uri())


def test_unusual_git_paths_non_utf8_and_non_regular_entries(repository):
    r = repository
    git(r.root, 'checkout', '-b', 'odd', r.base)
    # Index-only writes support paths Windows cannot represent in its working tree.
    names = ['a space.py', ':literal.py', 'unicode-\u03bb.py', 'tab\tname.py', 'line\nname.py']
    oid = git(r.root, 'hash-object', '-w', '--stdin', input=b'odd path\n')
    for name in names:
        git(r.root, 'update-index', '--add', '--cacheinfo', f'100644,{oid},{name}')
    link = git(r.root, 'hash-object', '-w', '--stdin', input=b'../../outside')
    git(r.root, 'update-index', '--add', '--cacheinfo', f'120000,{link},link')
    git(r.root, 'update-index', '--add', '--cacheinfo', f'160000,{r.base},module')
    invalid = git(r.root, 'hash-object', '-w', '--stdin', input=b'\xfftext')
    git(r.root, 'update-index', '--add', '--cacheinfo', f'100644,{invalid},non-utf8.py')
    git(r.root, 'commit', '-m', 'odd entries')
    head = git(r.root, 'rev-parse', 'HEAD')
    reader = PullRequestGitReader(r.source, r.settings)
    files = {f.path: f for f in reader.build_file_changes(r.root, r.base, head)}
    assert all(files[name].head_content == 'odd path\n' for name in names)
    assert files['link'].head_content_status == 'symlink'
    assert files['module'].head_content_status == 'submodule'
    assert files['non-utf8.py'].head_content_status == 'non_utf8'


def test_type_change_is_explicit(repository):
    r = repository
    git(r.root, 'checkout', '-b', 'type-change', r.base)
    link = git(r.root, 'hash-object', '-w', '--stdin', input=b'other.py')
    git(r.root, 'update-index', '--cacheinfo', f'120000,{link},service.py')
    git(r.root, 'commit', '-m', 'replace source with symlink')
    head = git(r.root, 'rev-parse', 'HEAD')
    files = PullRequestGitReader(r.source, r.settings).build_file_changes(r.root, r.base, head)
    assert len(files) == 1
    assert files[0].status == 'type_changed'
    assert files[0].head_content_status == 'symlink'
    assert files[0].patch_status == 'non_regular'


def test_settings_are_configurable_and_reject_invalid_limits(monkeypatch):
    monkeypatch.setenv('PR_MAX_FILE_BYTES', '73')
    monkeypatch.setenv('PR_WRITE_DEBUG_ARTIFACTS', 'true')
    settings = PullRequestSettings.from_env()
    assert settings.max_file_bytes == 73 and settings.write_debug_artifacts
    monkeypatch.setenv('PR_MAX_FILE_BYTES', '0')
    with pytest.raises(ValueError, match='positive'):
        PullRequestSettings.from_env()


@pytest.mark.asyncio
async def test_revision_dedup_artifact_metadata_only_and_accepted_state_unchanged(repository, store):
    instance = service(repository, store)
    instance.settings = replace(repository.settings, write_debug_artifacts=True)
    result = await instance.ingest_pull_request(repository.event)
    assert result.merge_base_sha == repository.base
    assert json.loads(instance.artifact_path(repository.event).read_text())['head_sha'] == repository.head
    assert await instance.ingest_pull_request(repository.event) is None
    assert store.get_pr_revision(repository.event.revision_key).status == 'COMPLETED'
    assert store.get_repository('repo_101').indexed_commit_sha == repository.target
    assert store.get_repository('repo_101').status == 'COMPLETED'
    with store.session_factory() as session:
        columns = {field['name'] for field in inspect(session.bind).get_columns('pull_request_revisions')}
        assert not columns & {'content', 'files', 'body', 'title', 'change_set', 'base_content', 'head_content'}
    instance.github_app.indexing_credential.assert_awaited()


@pytest.mark.asyncio
async def test_failed_revision_retry_atomic_running_claim_and_restart_recovery(repository, store):
    instance = service(repository, store)
    original = instance.reader.read_revision
    instance.reader.read_revision = Mock(side_effect=RuntimeError('fetch failed'))
    with pytest.raises(RuntimeError):
        await instance.ingest_pull_request(repository.event)
    assert store.get_pr_revision(repository.event.revision_key).status == 'FAILED'
    instance.reader.read_revision = original
    assert await instance.ingest_pull_request(repository.event)
    assert store.get_pr_revision(repository.event.revision_key).attempts == 2
    changed = repository.event.model_copy(update={'base_sha': repository.base})
    assert store.claim_pr_revision('repo_101', changed)
    assert not store.claim_pr_revision('repo_101', changed)
    store.recover_interrupted_pr_revisions()
    assert store.claim_pr_revision('repo_101', changed)


@pytest.mark.asyncio
async def test_invalid_clone_and_auth_denial_never_modify_accepted_repository(repository, store, tmp_path):
    instance = service(repository, store)
    instance.source._repo_clone_path = Mock(return_value=tmp_path / 'missing')
    with pytest.raises(PullRequestIngestionError):
        await instance.ingest_pull_request(repository.event)
    assert store.get_pr_revision(repository.event.revision_key).status == 'FAILED'
    assert store.get_repository('repo_101').indexed_commit_sha == repository.target
    instance.github_app.indexing_credential.side_effect = RuntimeError('access denied')
    with pytest.raises(RuntimeError):
        await instance.ingest_pull_request(repository.event)
    assert store.get_pr_revision(repository.event.revision_key).status == 'FAILED'


@pytest.mark.asyncio
async def test_untracked_and_installation_mismatch(repository, store):
    instance = service(repository, store)
    for updates in [{'github_repository_id': 999}, {'installation_id': 99}, {'repository_private': True}]:
        with pytest.raises(PullRequestIngestionError):
            await instance.ingest_pull_request(repository.event.model_copy(update=updates))
    instance.github_app.indexing_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_repository_lock_and_concurrent_duplicate(repository, store):
    lock = asyncio.Lock()
    instance = service(repository, store)
    instance.repository_lock = lambda _: lock
    instance.reader.read_revision = Mock(return_value=(repository.base, []))
    await lock.acquire()  # Simulate accepted RKG ingestion holding the clone lock.
    first = asyncio.create_task(instance.ingest_pull_request(repository.event))
    await asyncio.sleep(0)
    assert store.get_pr_revision(repository.event.revision_key).status == 'RUNNING'
    instance.reader.read_revision.assert_not_called()
    assert await instance.ingest_pull_request(repository.event) is None
    lock.release()
    assert await first
    instance.reader.read_revision.assert_called_once()


@pytest.mark.asyncio
async def test_access_revoked_during_git_read_cannot_publish(repository, store):
    instance = service(repository, store)
    instance.reader.read_revision = Mock(return_value=(repository.base, []))
    instance.github_app.indexing_credential.side_effect = [repository.credential, RuntimeError('revoked')]
    with pytest.raises(RuntimeError, match='revoked'):
        await instance.ingest_pull_request(repository.event)
    assert store.get_pr_revision(repository.event.revision_key).status == 'FAILED'
    assert not instance.artifact_path(repository.event).exists()


@pytest.mark.asyncio
async def test_cancellation_keeps_clone_lock_until_git_thread_exits(repository, store):
    instance = service(repository, store)
    lock = asyncio.Lock()
    instance.repository_lock = lambda _: lock
    started, release = threading.Event(), threading.Event()

    def read(*_):
        started.set()
        assert release.wait(timeout=5)
        return repository.base, []

    instance.reader.read_revision = read
    task = asyncio.create_task(instance.ingest_pull_request(repository.event))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert store.get_pr_revision(repository.event.revision_key).status == 'FAILED'


@pytest.mark.asyncio
async def test_out_of_order_revision_artifacts_are_separate(repository, store):
    instance = service(repository, store)
    instance.settings = replace(repository.settings, write_debug_artifacts=True)
    instance.reader.read_revision = Mock(return_value=(repository.base, []))
    newer = repository.event.model_copy(update={'head_sha': repository.target})
    await instance.ingest_pull_request(newer)
    await instance.ingest_pull_request(repository.event)
    assert instance.artifact_path(newer) != instance.artifact_path(repository.event)
    for event in [newer, repository.event]:
        assert json.loads(instance.artifact_path(event).read_text())['head_sha'] == event.head_sha
        assert store.get_pr_revision(event.revision_key).status == 'COMPLETED'


def payload(event):
    return {'action': event.source_event, 'installation': {'id': event.installation_id},
        'repository': {'id': event.github_repository_id, 'full_name': event.repository_full_name, 'private': False},
        'pull_request': {'number': event.pull_request_number, 'title': event.title, 'body': event.body,
            'base': {'sha': event.base_sha, 'ref': event.base_branch, 'repo': {'id': event.base_repository_id}},
            'head': {'sha': event.head_sha, 'ref': event.head_branch,
                     'repo': {'id': event.head_repository_id, 'full_name': event.head_repository_full_name}}}}


@pytest.fixture
def webhook_app(repository, store):
    app = FastAPI()
    app.include_router(router, prefix='/webhooks')
    app.state.github_app = SimpleNamespace(store=store, settings=SimpleNamespace(webhook_secret='test-secret'),
                                          indexing_credential=AsyncMock())
    app.state.pr_ingestion_service = SimpleNamespace(ingest_pull_request=AsyncMock())
    app.state.ingestion_service = SimpleNamespace(handle_github_pull_request_webhook=AsyncMock(
        return_value=SimpleNamespace(status='completed', repository='owner/repo', message='push handles merge', indexed_commit_sha=repository.target)))
    return app


def send(app, data, event='pull_request', signature=True):
    raw = json.dumps(data).encode()
    digest = hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        return client.post('/webhooks/github', content=raw, headers={'X-GitHub-Event': event,
            'X-GitHub-Delivery': 'delivery-42', 'X-Hub-Signature-256': 'sha256=' + (digest if signature else 'bad')})


@pytest.mark.parametrize('action', ['opened', 'synchronize', 'reopened'])
def test_supported_webhook_dispatches_normalized_event_without_user_auth_or_rkg(webhook_app, repository, action):
    data = payload(repository.event)
    data['action'] = action
    response = send(webhook_app, data)
    assert response.status_code == 202
    event = webhook_app.state.pr_ingestion_service.ingest_pull_request.call_args.args[0]
    assert event.delivery_id == 'delivery-42' and event.source_event == action
    assert event.head_sha == repository.head
    webhook_app.state.github_app.indexing_credential.assert_not_awaited()  # execution-time service validates
    webhook_app.state.ingestion_service.handle_github_pull_request_webhook.assert_not_awaited()


def test_unsupported_actions_and_merged_ack_preserved(webhook_app, repository):
    assert send(webhook_app, {'action': 'edited'}).json()['status'] == 'ignored'
    webhook_app.state.pr_ingestion_service.ingest_pull_request.assert_not_awaited()
    data = payload(repository.event)
    data['action'] = 'closed'
    data['pull_request']['merged'] = True
    assert send(webhook_app, data).status_code == 200
    webhook_app.state.ingestion_service.handle_github_pull_request_webhook.assert_awaited_once()
    webhook_app.state.pr_ingestion_service.ingest_pull_request.assert_not_awaited()


@pytest.mark.parametrize('invalid', ['installation', 'sha', 'repo', 'boolean_id'])
def test_malformed_pr_metadata_rejected(webhook_app, repository, invalid):
    data = payload(repository.event)
    if invalid == 'installation': del data['installation']
    if invalid == 'sha': data['pull_request']['head']['sha'] = '--bad-option'
    if invalid == 'repo': data['pull_request']['base']['repo'] = None
    if invalid == 'boolean_id': data['installation']['id'] = True
    assert send(webhook_app, data).status_code == 400
    webhook_app.state.pr_ingestion_service.ingest_pull_request.assert_not_awaited()


def test_untracked_webhook_bad_signature_and_failed_background_inspectable(webhook_app, repository, caplog):
    data = payload(repository.event)
    data['repository']['id'] = 999
    data['pull_request']['base']['repo']['id'] = 999
    assert send(webhook_app, data).json()['status'] == 'ignored'
    assert send(webhook_app, payload(repository.event), signature=False).status_code == 401
    webhook_app.state.pr_ingestion_service.ingest_pull_request.side_effect = RuntimeError('secret should not be logged')
    assert send(webhook_app, payload(repository.event)).status_code == 202
    assert 'delivery-42' in caplog.text and 'secret should not be logged' not in caplog.text


def test_additive_pr_schema_initialization_preserves_repository_data(tmp_path, monkeypatch):
    from ai_services.ingestion.persistence import database
    engine = create_engine(f'sqlite:///{tmp_path / "old-app.db"}')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE app_schema_version (version INTEGER NOT NULL)'))
        connection.execute(text('INSERT INTO app_schema_version VALUES (1)'))
        connection.execute(text('CREATE TABLE repositories (id TEXT PRIMARY KEY)'))
        connection.execute(text("INSERT INTO repositories VALUES ('repo_101')"))
    monkeypatch.setattr(database, 'get_engine', lambda: engine)
    database.init_db()
    database.init_db()
    with engine.connect() as connection:
        assert connection.execute(text('SELECT id FROM repositories')).scalar() == 'repo_101'
        assert connection.execute(text('SELECT count(*) FROM pull_request_revisions')).scalar() == 0
    engine.dispose()
