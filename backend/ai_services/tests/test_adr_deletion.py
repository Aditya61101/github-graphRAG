"""Local SQL, mock graph/storage tests; no production resource is contacted."""
# ruff: noqa: F811 -- shared fixtures
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from ai_services.ingestion.adr.jobs import ADRJobs
from ai_services.ingestion.adr.neo4j_writer import ADRNeo4jWriter
from ai_services.ingestion.adr.originals import OriginalStorage
from ai_services.ingestion.persistence.models import ADRModel, IngestionRunModel
from ai_services.ingestion.readiness import GraphReadBarrier, GraphUnavailable
from ai_services.tests.test_adr_upload import test_env, post_adr_batch
from ai_services.tests.test_async_adr_ingestion import durable, accept
from ai_services.tests.test_adr_neo4j_pipeline import MockNeo4jDriver, adr_env
from ai_services.tests.test_pr_orchestration import env


@pytest.fixture
def deletion_api(test_env):
    app = test_env['client'].app
    app.state.github_app.authorize_tracked = AsyncMock(
        side_effect=lambda user, identifier, **kw: test_env['store'].get_repository(identifier))
    response = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    record = response.json()['results'][0]['adr']
    return SimpleNamespace(**test_env, record=record,
        headers={'Authorization': f"Bearer {test_env['alice_jwt']}"},
        url=f"/repositories/{test_env['repo_alice_id']}/adrs/{record['id']}")


def test_admin_delete_commits_intent_hides_source_and_is_idempotent(deletion_api):
    e = deletion_api
    for _ in range(2):
        response = e.client.delete(e.url, headers=e.headers)
        assert response.status_code == 202 and response.json()['status'] == 'DELETED'
    store = e.store
    assert store.get_adr(e.record['id']) is None
    assert store.get_adr(e.record['id'], include_deleted=True).status == 'DELETED'
    assert not store.list_adrs(e.record['repository_id'])
    assert e.client.get(e.url + '/status', headers=e.headers).status_code == 404
    assert e.client.post(e.url + '/retry', headers=e.headers).status_code == 404
    run = ADRJobs(store.session_factory).for_adr(e.record['id'])
    assert run.trigger == 'adr_delete' and run.status == 'QUEUED'
    calls = e.client.app.state.github_app.authorize_tracked.call_args_list
    assert sum(call.kwargs == {'require_admin': True} for call in calls) == 2
    # Original still exists: the HTTP handler did no physical deletion.
    from pathlib import Path
    assert Path(store.get_adr(e.record['id'], include_deleted=True).file_path).exists()


def test_unauthenticated_collaborator_and_cross_repository_delete_rejected(deletion_api):
    e = deletion_api
    assert e.client.delete(e.url).status_code == 401
    bob = {'Authorization': f"Bearer {e.bob_jwt}"}
    assert e.client.delete(e.url, headers=bob).status_code == 403
    wrong = e.url.replace(e.repo_alice_id, e.repo_bob_id)
    assert e.client.delete(wrong, headers=bob).status_code == 404
    e.client.app.state.github_app.authorize_tracked.side_effect = HTTPException(403, 'Admin required')
    assert e.client.delete(e.url, headers=e.headers).status_code == 403
    assert e.store.get_adr(e.record['id']) is not None


def test_reupload_after_deletion_has_a_new_identity(deletion_api):
    e = deletion_api
    assert e.client.delete(e.url, headers=e.headers).status_code == 202
    fresh = post_adr_batch(vars(e), [('one.md', b'# One', 'text/markdown')])
    assert fresh.status_code == 202
    assert fresh.json()['results'][0]['adr']['id'] != e.record['id']
    assert e.store.get_adr(e.record['id']) is None


def test_two_deletions_share_one_durable_intent(env):
    record = env.application_store.create_adr('repo_101', 'Policy', 'one.md', 'unused', 'hash')
    jobs = ADRJobs(env.sessions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: jobs.delete_adr('repo_101', record.id), range(2)))
    assert results == [record.id, record.id]
    with env.sessions() as s:
        assert len(list(s.scalars(select(IngestionRunModel).where(
            IngestionRunModel.trigger == 'adr_delete')))) == 1


def test_failed_cleanup_repeated_delete_requeues_without_reviving_source(deletion_api):
    e = deletion_api
    e.client.delete(e.url, headers=e.headers)
    jobs = ADRJobs(e.store.session_factory)
    with jobs.sessions.begin() as s:
        run = s.get(IngestionRunModel, e.record['id'])
        run.status, run.attempts = 'FAILED', 3
    assert e.client.delete(e.url, headers=e.headers).status_code == 202
    assert jobs.get(e.record['id']).status == 'QUEUED'
    assert jobs.get(e.record['id']).attempts == 0
    assert e.store.get_adr(e.record['id']) is None


@pytest.mark.parametrize('code', [204, 404])
def test_gcs_delete_missing_is_success_and_scope_is_strict(tmp_path, code):
    session = SimpleNamespace(delete=Mock(return_value=SimpleNamespace(
        status_code=code, raise_for_status=Mock())))
    originals = OriginalStorage(tmp_path, bucket='bucket', session=session)
    location = originals.location('repo', 'adr_one', '.md')
    originals.delete(location, 'repo', 'adr_one')
    assert 'repo%2Fadr_one%2Foriginal.md' in session.delete.call_args.args[0]
    for wrong_repo, wrong_id in [('other', 'adr_one'), ('repo', 'adr_other')]:
        with pytest.raises(ValueError):
            originals.delete(location, wrong_repo, wrong_id)
    assert session.delete.call_count == 1


def install_delete_interpreter(driver):
    """Extend the established in-memory Cypher fixture for cleanup transactions."""
    original = driver._execute_cypher

    def remove(key):
        driver.nodes.pop(key, None)
        driver.relationships[:] = [r for r in driver.relationships
            if r['source'] != key and r['target'] != key]

    def execute(query, params):
        if '// ADR_DELETE_' not in query:
            return original(query, params)
        driver.calls.append((query, params))
        assert 'repository:$repo' in query
        repo, adr = params['repo'], params['adr_id']
        if 'ADR_DELETE_OWNED' in query:
            for key, node in list(driver.nodes.items()):
                if node.get('repository') != repo:
                    continue
                label = node['_label']
                if not ((label in {'Chunk', 'ArchitecturalDecision', 'ArchitecturalConstraint'} and node.get('adr_id') == adr)
                        or (label == 'GraphAssertion' and node.get('source_type') == 'ADR' and node.get('source_id') == adr)):
                    continue
                supporters = {r['source'] for r in driver.relationships if r['target'] == key and r['type'] in {'DEFINES', 'HAS_CHUNK'}}
                supporting_chunks = {r['source'] for r in driver.relationships if r['target'] == key and r['type'] == 'SUPPORTS'}
                supporters.update(r['source'] for r in driver.relationships if r['target'] in supporting_chunks and r['type'] == 'HAS_CHUNK')
                owners = sorted(driver.nodes[k]['id'] for k in supporters
                    if k in driver.nodes and driver.nodes[k].get('repository') == repo
                    and driver.nodes[k]['_label'] == 'ADR' and driver.nodes[k]['id'] != adr)
                if owners:
                    node['source_id' if label == 'GraphAssertion' else 'adr_id'] = owners[0]
                else:
                    remove(key)
        elif 'ADR_DELETE_DOCUMENT' in query:
            key = f'ADR:{adr}'
            if driver.nodes.get(key, {}).get('repository') == repo:
                remove(key)
        elif 'ADR_DELETE_INDEXES' in query:
            for key, node in list(driver.nodes.items()):
                if node.get('_label') == 'DGRetrievalIndex' and node.get('repository') == repo and adr in node.get('payload_json', ''):
                    remove(key)
        else:
            for key, node in list(driver.nodes.items()):
                if node.get('_label') != 'Entity' or node.get('repository') != repo or node.get('source') != 'ADR':
                    continue
                owner = driver.nodes.get(f"ADR:{node.get('source_id')}")
                if (node.get('source_id') == adr or not owner) and not any(
                    key in (r['source'], r['target']) and r['type'] != 'MEMBER_OF' for r in driver.relationships):
                    remove(key)
        return [], None, None
    driver._execute_cypher = execute


def graph_fixture():
    driver = MockNeo4jDriver()
    def node(label, identity, **props):
        key = f'{label}:{identity}'
        driver.nodes[key] = dict(_label=label, id=identity, repository='repo', **props)
        return key
    a, b = node('ADR', 'a'), node('ADR', 'b')
    own = node('Chunk', 'own', adr_id='a', embedding=[1.0])
    shared = node('ArchitecturalDecision', 'shared', adr_id='a')
    support = node('Chunk', 'support', adr_id='b')
    exclusive = node('ArchitecturalConstraint', 'exclusive', adr_id='a')
    entity = node('Entity', 'shared', source='ADR', source_id='a')
    orphan = node('Entity', 'orphan', source='ADR', source_id='a')
    code = node('Entity', 'code', source='CODE')
    assertion = node('GraphAssertion', 'a-edge', source_type='ADR', source_id='a')
    for source, kind, target in [(a,'HAS_CHUNK',own),(a,'DEFINES',shared),
        (b,'HAS_CHUNK',support),(support,'SUPPORTS',shared),(support,'MENTIONS',entity),
        (a,'DEFINES',exclusive),(own,'MENTIONS',orphan),(own,'SUPPORTS',assertion)]:
        driver._add_relationship(dict(source=source, type=kind, target=target))
    driver.nodes['ADR:foreign'] = dict(_label='ADR', id='foreign', repository='other')
    node('DGRetrievalIndex', 'old', payload_json='{"adr_id":"a"}')
    install_delete_interpreter(driver)
    return driver


def test_graph_cleanup_removes_exclusive_vectors_preserves_shared_and_code():
    driver = graph_fixture()
    writer = ADRNeo4jWriter(driver)
    for _ in range(2):
        writer.delete_adr('a', 'repo')
    assert {'ADR:b', 'Chunk:support', 'ArchitecturalDecision:shared', 'Entity:shared', 'Entity:code', 'ADR:foreign'} == set(driver.nodes)
    assert driver.nodes['ArchitecturalDecision:shared']['adr_id'] == 'b'
    assert any(r['target'] == 'ArchitecturalDecision:shared' for r in driver.relationships)
    writer.delete_adr('b', 'repo')
    assert set(driver.nodes) == {'Entity:code', 'ADR:foreign'}


def test_graph_cleanup_failure_rolls_back_transaction():
    driver = graph_fixture()
    before = copy.deepcopy((driver.nodes, driver.relationships))
    driver.fail_queries_matching = ['ADR_DELETE_DOCUMENT']
    with pytest.raises(RuntimeError):
        ADRNeo4jWriter(driver).delete_adr('a', 'repo')
    assert (driver.nodes, driver.relationships) == before


@pytest.fixture
def deleting(durable):
    install_delete_interpreter(durable.driver)
    durable.control.seal = lambda run: durable.seal.update(state='COMPLETE')
    return durable


async def test_queued_ingestion_cannot_reactivate_deleted_adr_and_cleanup_recovers(deleting):
    e = deleting
    record = await accept(e)
    old_id = e.jobs.for_adr(record.id).id
    delete_id = e.jobs.delete_adr('repo_a', record.id)
    assert e.jobs.get(old_id).status == 'SUPERSEDED'
    assert e.jobs.mapping(old_id).adr_id == record.id
    assert e.jobs.claim(old_id) is None
    e.store.update_adr_status(record.id, 'COMPLETED')
    with pytest.raises(ValueError):
        e.jobs.enqueue_adr(record.id, retry=True)
    assert e.store.get_adr(record.id) is None
    with pytest.raises(GraphUnavailable):
        GraphReadBarrier(e.jobs.sessions, e.control).snapshot('repo_a')
    await e.worker.run(delete_id)
    assert e.jobs.get(delete_id).status == 'COMPLETED'
    assert e.store.get_adr(record.id, include_deleted=True).status == 'DELETED'
    assert not e.service.originals._local(record.file_path, 'repo_a', record.id).exists()
    GraphReadBarrier(e.jobs.sessions, e.control).snapshot('repo_a')


async def test_running_ingestion_completion_cannot_revive_tombstone(deleting):
    e = deleting
    record = await accept(e)
    old = e.jobs.claim(e.jobs.for_adr(record.id).id)
    e.control.arm(old)
    delete_id = e.jobs.delete_adr('repo_a', record.id)
    assert e.jobs.claim(delete_id) is None  # Live writer retains its fence.
    e.jobs.finish(old.id, old.owner, old.target_commit_sha)
    assert e.store.get_adr(record.id) is None
    e.seal['state'] = 'COMPLETE'
    await e.worker.run(delete_id)
    assert e.jobs.get(delete_id).status == 'COMPLETED'


async def test_failed_storage_cleanup_keeps_tombstone_and_durable_retry(deleting, monkeypatch):
    e = deleting
    record = await accept(e)
    run_id = e.jobs.delete_adr('repo_a', record.id)
    original = e.worker.originals.delete
    monkeypatch.setattr(e.worker.originals, 'delete', Mock(side_effect=OSError('storage unavailable')))
    with pytest.raises(OSError):
        await e.worker.run(run_id)
    assert e.jobs.get(run_id).status == 'RETRY'
    assert e.store.get_adr(record.id) is None
    with pytest.raises(GraphUnavailable):
        GraphReadBarrier(e.jobs.sessions, e.control).snapshot('repo_a')
    e.clock[0] += 301
    monkeypatch.setattr(e.worker.originals, 'delete', original)
    await e.worker.run(run_id)
    assert e.jobs.get(run_id).status == 'COMPLETED'


async def test_expired_old_ingestion_drains_without_reextracting_deleted_document(deleting):
    e = deleting
    record = await accept(e)
    old = e.jobs.claim(e.jobs.for_adr(record.id).id)
    e.control.arm(old)
    delete_id = e.jobs.delete_adr('repo_a', record.id)
    e.extractor.extract_all = AsyncMock(side_effect=AssertionError('Deleted ADR was extracted'))
    e.clock[0] += 121
    await e.worker.run(old.id)
    assert e.jobs.get(old.id).status == 'COMPLETED'
    assert e.store.get_adr(record.id) is None
    e.extractor.extract_all.assert_not_awaited()
    await e.worker.run(delete_id)
    assert e.jobs.get(delete_id).status == 'COMPLETED'


async def test_legacy_processor_rejects_deleted_document_before_graph_work():
    from ai_services.ingestion.adr.guard import GuardedADRProcessor
    store = SimpleNamespace(get_adr=lambda _: None)
    barrier = Mock()
    processor = GuardedADRProcessor(Mock(), store, Mock(), Mock(), barrier, None, None)
    with pytest.raises(ValueError, match='deleted'):
        await processor.process_adr(SimpleNamespace(adr_id='deleted'))
    barrier.snapshot.assert_not_called()


async def test_deletion_recovery_uses_existing_adr_job_and_retains_sealed_work(deleting, monkeypatch):
    from ai_services.ingestion.dispatch import reconcile
    e = deleting
    record = await accept(e)
    run_id = e.jobs.delete_adr('repo_a', record.id)
    dispatcher = SimpleNamespace(dispatch=AsyncMock(), dispatch_adr=AsyncMock())
    await reconcile(e.jobs, dispatcher)
    dispatcher.dispatch_adr.assert_awaited_once_with(run_id)
    dispatcher.dispatch.assert_not_awaited()
    finish = e.jobs.finish
    monkeypatch.setattr(e.jobs, 'finish', Mock(side_effect=RuntimeError('SQL unavailable')))
    with pytest.raises(RuntimeError):
        await e.worker.run(run_id)
    assert e.jobs.get(run_id).status == 'INDEXING'
    monkeypatch.setattr(e.jobs, 'finish', finish)
    await e.worker.run(run_id)
    assert e.jobs.get(run_id).status == 'COMPLETED'
    assert e.community.await_count == 1


def test_new_evaluation_rejects_deleted_sources_even_after_cleanup(env):
    from ai_services.decision_guard.evaluation import UploadAuthority
    record = env.application_store.create_adr('repo_101', 'Policy', 'one.md', 'unused', 'hash')
    jobs = ADRJobs(env.sessions)
    jobs.delete_adr('repo_101', record.id)
    context = {'adr_snapshot': [{'adr_id': record.id}]}
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    token = env.store.claim(job_id)
    env.store.checkpoint(job_id, token, 'CONTEXT_BUILT', context_json=json.dumps(context))
    for completed in [False, True]:
        if completed:
            with env.sessions.begin() as s:
                s.get(IngestionRunModel, record.id).status = 'COMPLETED'
        with pytest.raises(ValueError, match='deleted ADR'):
            env.store.cached_packet(job_id, token, 'cached')
        with pytest.raises(ValueError, match='deleted ADR'):
            env.store.reserve_packet(job_id, token, 'new', 'ASSESS', '{}', 100, 0)
    authority = UploadAuthority(env.application_store, '.')
    with pytest.raises(ValueError, match='deleted ADR'):
        authority.receipts(SimpleNamespace(revision=SimpleNamespace(repository_id='repo_101'),
            adr_snapshot=[SimpleNamespace(adr_id=record.id)]))
    assert env.store.get(job_id).provider_attempts == 0


async def test_historical_evaluation_and_publication_are_not_rewritten(env):
    from ai_services.decision_guard.orchestration.read_models import CheckPublication
    job_id = env.store.enqueue('repo_101', env.event, env.settings)
    await env.worker.run_job(job_id)
    old = env.store.get(job_id)
    with env.sessions() as s:
        publication = s.get(CheckPublication, job_id)
        prior_publication = (publication.status, publication.evaluation_id, publication.check_run_id)
    record = env.application_store.create_adr('repo_101', 'Policy', 'one.md', 'unused', 'hash')
    ADRJobs(env.sessions).delete_adr('repo_101', record.id)
    current = env.store.get(job_id)
    assert (current.result_json, current.status, current.provider_attempts) == (
        old.result_json, old.status, old.provider_attempts)
    with env.sessions() as s:
        publication = s.get(CheckPublication, job_id)
        assert prior_publication == (publication.status, publication.evaluation_id, publication.check_run_id)


@pytest.mark.parametrize('case', ['current', 'empty', 'changed', 'missing_embedding',
    'stale_embedding', 'empty_community', 'unassigned', 'refresh_failure'])
async def test_deletion_community_reuse_rechecks_persisted_state(monkeypatch, case):
    from ai_services.ingestion import service as module
    from ai_services.ingestion.community.models import CommunityContext, CommunityMember
    from ai_services.ingestion.community.embedder import compute_embedding_version_hash
    context = CommunityContext(community_id='c', members=[
        CommunityMember(entity_id='entity', entity_name='Component', labels=['Entity'])])
    state = SimpleNamespace(has_summary=True, summary='Existing summary',
        architectural_role='Component role', summary_hash='summary-hash',
        context_hash=context.context_hash(), has_embedding=True,
        embedded_summary_hash=compute_embedding_version_hash('summary-hash', True))
    ids, contexts = ['c'], [context]
    if case == 'changed':
        state.context_hash = 'previous-context'
    elif case == 'missing_embedding':
        state.has_embedding = False
    elif case == 'stale_embedding':
        state.embedded_summary_hash = 'previous-embedding'
    elif case == 'empty_community':
        contexts = []
    elif case == 'unassigned':
        ids.append(None)
    elif case == 'empty':
        ids, contexts = [], []
    elif case == 'refresh_failure':
        state.context_hash = 'previous-context'
    pipeline = SimpleNamespace(store=SimpleNamespace(
        load_community_contexts=AsyncMock(return_value=contexts),
        get_processing_states=AsyncMock(return_value={'c': state})), run=AsyncMock())
    if case == 'refresh_failure':
        pipeline.run.side_effect = RuntimeError('refresh failed')
    monkeypatch.setattr(module, 'build_community_pipeline', lambda **_: pipeline)
    service = object.__new__(module.RepositoryIngestionService)
    service.llm, service.embedder, service.database = object(), object(), 'neo4j'
    service.neo4j_driver = Mock()
    service.neo4j_driver.execute_query.return_value = SimpleNamespace(
        records=[{'community_id': cid} for cid in ids])
    if case == 'refresh_failure':
        with pytest.raises(RuntimeError, match='refresh failed'):
            await service._run_community_pipeline('repo', reuse_unchanged=True)
    else:
        await service._run_community_pipeline('repo', reuse_unchanged=True)
    if case in {'current', 'empty'}:
        pipeline.run.assert_not_awaited()
    else:
        pipeline.run.assert_awaited_once_with(force_refresh=False)


async def test_required_community_refresh_failure_does_not_seal_deletion(deleting):
    e = deleting
    record = await accept(e)
    run_id = e.jobs.delete_adr('repo_a', record.id)
    e.community.side_effect = RuntimeError('community unavailable')
    with pytest.raises(RuntimeError, match='community unavailable'):
        await e.worker.run(run_id)
    e.community.assert_awaited_once_with('repo_a', reuse_unchanged=True)
    assert e.seal['state'] == 'BUILDING'
    assert e.jobs.get(run_id).status == 'RETRY'
    e.community.side_effect = None
    e.clock[0] += 301
    await e.worker.run(run_id)
    assert e.jobs.get(run_id).status == 'COMPLETED'
