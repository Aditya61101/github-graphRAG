"""ADR acceptance/recovery contracts. SQLite + in-memory Cypher fixture only."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from ai_services.ingestion.adr.jobs import ADRJobs
from ai_services.ingestion.adr.worker import ADRWorker
from ai_services.ingestion.persistence.models import ADRModel, RepositoryModel, RepositoryIngestionState
from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs, LostIngestionLease
from ai_services.tests.test_adr_upload import test_env, post_adr_batch
from ai_services.tests.test_adr_neo4j_pipeline import adr_env


def test_acceptance_does_not_parse_or_extract_and_survives_dispatch_failure(test_env, monkeypatch):
    from api_services.app.routers import repositories
    from ai_services.ingestion import dispatch
    # Restore the actual bounded dispatch helper overridden by the shared fixture.
    actual = _real_dispatch
    monkeypatch.setattr(repositories, '_dispatch_adrs', actual)
    monkeypatch.setattr(dispatch, 'configured_dispatcher', Mock(side_effect=RuntimeError('private credential')))
    test_env['adr_service'].processor = SimpleNamespace(process_adr=AsyncMock(side_effect=AssertionError('inline work')))
    result = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    assert result.status_code == 202 and 'private credential' not in result.text
    adr = result.json()['results'][0]['adr']
    assert adr['status'] == 'PENDING' and result.json()['completed'] == 0
    jobs = ADRJobs(test_env['store'].session_factory)
    run = jobs.for_adr(adr['id'])
    assert run.trigger == 'adr' and run.id in jobs.due()
    # A new process reconstructs intent purely from SQL.
    assert ADRJobs(test_env['store'].session_factory).for_adr(adr['id']).id == run.id
    test_env['adr_service'].processor.process_adr.assert_not_awaited()


from api_services.app.routers.repositories import _dispatch_adrs as _real_dispatch


def test_identical_uploads_are_idempotent_and_documents_do_not_coalesce(test_env):
    first = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    duplicate = post_adr_batch(test_env, [('copy.md', b'# One', 'text/markdown')])
    other = post_adr_batch(test_env, [('two.md', b'# Two', 'text/markdown')])
    ids = [r.json()['results'][0]['adr']['id'] for r in (first, duplicate, other)]
    assert ids[0] == ids[1] and ids[0] != ids[2]
    jobs = ADRJobs(test_env['store'].session_factory)
    assert jobs.for_adr(ids[0]).id != jobs.for_adr(ids[2]).id


def test_status_is_read_only_authorized_and_sanitized(test_env):
    response = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    adr_id = response.json()['results'][0]['adr']['id']
    base = f"/repositories/{test_env['repo_alice_id']}/adrs/{adr_id}"
    test_env['store'].update_adr_status(adr_id, 'FAILED', error='private credential')
    headers = {'Authorization': f"Bearer {test_env['alice_jwt']}"}
    result = test_env['client'].get(base + '/status', headers=headers)
    assert result.status_code == 200 and result.json()['status'] == 'FAILED'
    assert 'private credential' not in result.text and 'stage' not in result.json()
    assert test_env['store'].get_adr(adr_id).error == 'private credential'
    assert test_env['client'].get(base + '/status').status_code == 401
    wrong = base.replace(test_env['repo_alice_id'], test_env['repo_bob_id'])
    assert test_env['client'].get(wrong + '/status', headers=headers).status_code in {403, 404}
    assert test_env['client'].get(base.replace(adr_id, 'absent') + '/status', headers=headers).status_code == 404


@pytest.fixture
def durable(adr_env, monkeypatch):
    from ai_services.ingestion import service as ingestion_service
    from ai_services.ingestion.adr import worker as worker_module
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'true')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.delenv('ADR_GCS_BUCKET', raising=False)
    store = adr_env['store']
    with store.session_factory.begin() as s:
        repo = s.get(RepositoryModel, 'repo_a')
        repo.indexed_commit_sha, repo.installation_id = 'a' * 40, '10'
        s.add(RepositoryIngestionState(repository_id=repo.id, state='READY', epoch=1,
            published_run_id='baseline', published_token=1))
    clock = [int(time.time()) + 60]
    jobs = ADRJobs(store.session_factory, clock=lambda: clock[0])
    seal = dict(state='COMPLETE', run_id='baseline', token=1, target='a' * 40)
    def arm(run):
        seal.update(state='BUILDING', run_id=run.id, token=run.token, target=run.target_commit_sha)
    def publish(run, adr_id):
        assert store.get_adr(adr_id).status == 'PROCESSING'
        assert community.await_count
        seal['state'] = 'COMPLETE'
        adr_env['driver'].nodes[f'ADR:{adr_id}']['status'] = 'PUBLISHED'
    control = SimpleNamespace(database='neo4j', snapshot=lambda _: dict(seal), arm=arm,
                              seal_adr=publish, renew=lambda *_: None)
    # Existing Cypher interpreter verifies node/relationship writes and rollback;
    # repository fencing itself is covered in test_durable_ingestion.
    monkeypatch.setattr(worker_module, 'FencedDriver', lambda *_: adr_env['driver'])
    community = AsyncMock()
    monkeypatch.setattr(ingestion_service.RepositoryIngestionService, '_run_community_pipeline', community)
    github = SimpleNamespace(indexing_credential=AsyncMock())
    worker = ADRWorker(jobs, store, adr_env['service'].originals, adr_env['processor'], control,
                       llm=object(), embedder=adr_env['embedder'], github=github)
    return SimpleNamespace(**adr_env, jobs=jobs, worker=worker, clock=clock, seal=seal,
                           control=control, community=community)


async def accept(env, number=1):
    return await env.service.accept_upload('repo_a', 'usr_alice', f'{number}.md',
        f'# ADR {number}\n\n## Decision\nUse events {number}.'.encode())


async def test_three_sequential_documents_keep_earlier_graph_and_complete_after_publication(durable):
    from ai_services.ingestion.adr.models import ADRExtractionResult, ADRDecisionOutput
    durable.extractor.result = ADRExtractionResult(decisions=[ADRDecisionOutput(
        title='Use events', description='Persist events', affects=['Orders'])])
    ids = []
    for number in range(1, 4):
        record = await accept(durable, number)
        run = durable.jobs.for_adr(record.id)
        assert durable.store.get_adr(record.id).status == 'PENDING'
        await durable.worker.run(run.id)
        assert durable.store.get_adr(record.id).status == 'COMPLETED'
        ids.append(record.id)
        assert all(f'ADR:{adr}' in durable.driver.nodes for adr in ids)
    assert len({durable.jobs.for_adr(adr).id for adr in ids}) == 3


async def test_decision_identity_is_chunk_scoped_and_stable(durable):
    from ai_services.ingestion.adr.models import ADRExtractionResult, ADRDecisionOutput
    durable.extractor.result = ADRExtractionResult(decisions=[ADRDecisionOutput(
        title='Identical', description='One requirement')])
    record = await durable.service.accept_upload('repo_a', 'usr_alice', 'chunks.md',
        b'# Policy\n\n## First\nChoose A.\n\n## Second\nChoose B.')
    await durable.worker.run(durable.jobs.for_adr(record.id).id)
    # Fixture stores labels separately; inspect actual writer payload as authoritative.
    calls = [params['decisions'] for query, params in durable.driver.calls if 'decisions' in params]
    decisions = calls[-1]
    assert len(decisions) >= 2
    assert len({d['id'] for d in decisions}) == len(decisions)
    original = [d['id'] for d in decisions]
    from ai_services.ingestion.adr.parser import parse_adr_content
    doc = parse_adr_content(file_bytes=b'# Policy\n\n## First\nChoose A.\n\n## Second\nChoose B.',
        file_extension='.md', source_name='chunks.md', adr_id=record.id, repository_id='repo_a',
        file_path=record.file_path, content_hash=record.content_hash)
    await durable.processor.process_adr(doc)
    repeated = [params['decisions'] for query, params in durable.driver.calls if 'decisions' in params][-1]
    assert [d['id'] for d in repeated] == original


@pytest.mark.parametrize('phase', ['extraction', 'graph', 'community', 'seal'])
async def test_failure_retries_same_document_without_code_rebuild(durable, phase):
    record = await accept(durable)
    run_id = durable.jobs.for_adr(record.id).id
    if phase == 'extraction':
        durable.extractor.should_fail = True
    elif phase == 'graph':
        durable.driver.fail_queries_matching = ['MERGE (adr:ADR']
    elif phase == 'community':
        durable.community.side_effect = RuntimeError('private provider value')
    else:
        durable.control.seal_adr = Mock(side_effect=RuntimeError('seal failed'))
    with pytest.raises(Exception):
        await durable.worker.run(run_id)
    assert durable.store.get_adr(record.id).status == 'PENDING'
    assert durable.jobs.get(run_id).status == 'RETRY'
    assert 'private provider' not in (durable.store.get_adr(record.id).error or '')
    durable.extractor.should_fail = False
    durable.driver.fail_queries_matching = []
    durable.community.side_effect = None
    if phase == 'seal':
        durable.control.seal_adr = lambda run, _: durable.seal.update(state='COMPLETE')
    durable.clock[0] += 301
    await durable.worker.run(run_id)
    assert durable.store.get_adr(record.id).status == 'COMPLETED'
    assert durable.jobs.for_adr(record.id).id == run_id


async def test_sealed_graph_recovers_sql_commit_failure(durable, monkeypatch):
    record = await accept(durable)
    run_id = durable.jobs.for_adr(record.id).id
    finish = durable.jobs.finish
    monkeypatch.setattr(durable.jobs, 'finish', Mock(side_effect=RuntimeError('commit unavailable')))
    with pytest.raises(RuntimeError):
        await durable.worker.run(run_id)
    assert durable.jobs.get(run_id).status == 'INDEXING'
    assert durable.store.get_adr(record.id).status == 'PROCESSING'
    monkeypatch.setattr(durable.jobs, 'finish', finish)
    await durable.worker.run(run_id)
    assert durable.store.get_adr(record.id).status == 'COMPLETED'
    assert durable.community.await_count == 1


async def test_expired_owner_cannot_complete_after_reclaim(durable):
    record = await accept(durable)
    run_id = durable.jobs.for_adr(record.id).id
    old = durable.jobs.claim(run_id)
    assert durable.jobs.claim(run_id) is None
    durable.clock[0] += 121
    new = durable.jobs.claim(run_id)
    assert new.token > old.token
    with pytest.raises(LostIngestionLease):
        durable.jobs.finish(run_id, old.owner, old.target_commit_sha)


async def test_recovery_routes_adr_only_to_adr_job(durable):
    from ai_services.ingestion.dispatch import reconcile
    record = await accept(durable)
    dispatcher = SimpleNamespace(dispatch=AsyncMock(), dispatch_adr=AsyncMock())
    await reconcile(durable.jobs, dispatcher)
    dispatcher.dispatch.assert_not_awaited()
    dispatcher.dispatch_adr.assert_awaited_once_with(durable.jobs.for_adr(record.id).id)
    from ai_services.ingestion.worker import IngestionWorker
    worker = IngestionWorker(durable.jobs, None, None, None, None, llm=None, embedder=None)
    with pytest.raises(RuntimeError, match='ADR'):
        await worker.run(durable.jobs.for_adr(record.id).id)


async def test_processing_disabled_blocks_worker(durable, monkeypatch):
    record = await accept(durable)
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'false')
    with pytest.raises(RuntimeError, match='disabled'):
        await durable.worker.run(durable.jobs.for_adr(record.id).id)
    assert durable.jobs.for_adr(record.id).attempts == 0


def test_retry_reuses_original_and_active_intent(test_env):
    response = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    record = test_env['store'].get_adr(response.json()['results'][0]['adr']['id'])
    jobs = ADRJobs(test_env['store'].session_factory)
    run_id = jobs.for_adr(record.id).id
    with jobs.sessions.begin() as s:
        run = s.get(type(jobs.get(run_id)), run_id)
        run.status, run.attempts = 'FAILED', 3
        s.get(ADRModel, record.id).status = 'FAILED'
    headers = {'Authorization': f"Bearer {test_env['alice_jwt']}"}
    url = f'/repositories/{record.repository_id}/adrs/{record.id}/retry'
    for _ in range(2):
        response = test_env['client'].post(url, headers=headers)
        assert response.status_code == 202 and response.json()['status'] == 'PENDING'
        assert jobs.for_adr(record.id).id == run_id
    assert test_env['store'].get_adr(record.id).file_path == record.file_path
    test_env['store'].update_adr_status(record.id, 'COMPLETED')
    assert test_env['client'].post(url, headers=headers).status_code == 409


async def test_terminal_failure_does_not_block_another_adr_or_publish_failed_knowledge(durable):
    good = await accept(durable, 1)
    await durable.worker.run(durable.jobs.for_adr(good.id).id)
    bad = await accept(durable, 2)
    bad_run = durable.jobs.for_adr(bad.id).id
    durable.community.side_effect = RuntimeError('community failure')
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await durable.worker.run(bad_run)
        durable.clock[0] += 301
    assert durable.store.get_adr(bad.id).status == 'FAILED'
    durable.community.side_effect = None
    new = await accept(durable, 3)
    await durable.worker.run(durable.jobs.for_adr(new.id).id)
    assert durable.store.get_adr(new.id).status == 'COMPLETED'
    assert durable.driver.nodes[f'ADR:{good.id}']['status'] == 'PUBLISHED'
    assert durable.driver.nodes[f'ADR:{bad.id}']['status'] == 'FAILED'
    assert not any(n.get('adr_id') == bad.id for n in durable.driver.nodes.values())


async def test_hard_crash_lease_expiry_is_recoverable_and_bounded(durable):
    record = await accept(durable)
    run_id = durable.jobs.for_adr(record.id).id
    for _ in range(3):
        run = durable.jobs.claim(run_id)
        assert run
        durable.control.arm(run)
        durable.clock[0] += 121
    await durable.worker.run(run_id)
    assert durable.store.get_adr(record.id).status == 'FAILED'
    assert run_id not in durable.jobs.due()
    durable.jobs.enqueue_adr(record.id, retry=True)
    await durable.worker.run(run_id)
    assert durable.store.get_adr(record.id).status == 'COMPLETED'


async def test_adr_dispatch_payload_and_target_are_dedicated(monkeypatch):
    from ai_services.ingestion.dispatch import IngestionJobsDispatcher
    monkeypatch.setenv('ADR_INGESTION_JOB_RESOURCE', 'projects/p/locations/r/jobs/adr-worker')
    response = Mock(status_code=200)
    http = SimpleNamespace(post=AsyncMock(return_value=response))
    dispatcher = IngestionJobsDispatcher(http, AsyncMock(return_value='synthetic-token'),
                                        'projects/p/locations/r/jobs/repository-worker')
    await dispatcher.dispatch_adr('run_test')
    call = http.post.await_args
    assert call.args[0] == 'https://run.googleapis.com/v2/projects/p/locations/r/jobs/adr-worker:run'
    override = call.kwargs['json']['overrides']['containerOverrides'][0]
    assert override['args'] == ['-m', 'ai_services.runs.run_adr_ingestion']
    assert override['env'] == [{'name': 'INGESTION_RUN_ID', 'value': 'run_test'}]


async def test_job_entrypoint_disabled_before_initializing_dependencies(monkeypatch):
    from ai_services.runs import run_adr_ingestion
    monkeypatch.setenv('ENABLE_BACKGROUND_PROCESSING', 'false')
    monkeypatch.setattr(run_adr_ingestion, 'SqliteApplicationStore', Mock(side_effect=AssertionError('database opened')))
    with pytest.raises(RuntimeError, match='disabled'):
        await run_adr_ingestion.run()
