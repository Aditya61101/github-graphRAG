"""Offline boundaries, ownership and retry-cost regressions for Gemini ingestion."""
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from ai_services.ingestion.rkg.batching import TokenBudgetBatcher, extraction_batch_config
from ai_services.ingestion.rkg.models import EvidenceChunk, ChunkStrategy
from ai_services.ingestion.rkg.splitters import RepositoryTextSplitter
from ai_services.ingestion.rkg.llm_adapters import StructuredExtractor, ExtractionOutput
from ai_services.ingestion.persistence.models import IngestionRunModel, RepositoryIngestionState, RepositoryModel
from ai_services.tests.test_durable_ingestion import jobs, enqueue, A, B
from ai_services.tests.test_google_migration import config, response
from shared.ai.vertex import VertexLLM
from shared.ai.observability import error_classification, logger


def chunk(text, index=0):
    return EvidenceChunk.create("repo", A, "src/private.py", index, text, ChunkStrategy.WHOLE_FILE)


def batcher(provider="google"):
    return TokenBudgetBatcher(lambda text: len(text.encode("utf-8")), extraction_batch_config(provider))


@pytest.mark.parametrize("texts,lengths", [(["a"*3000,"b"*3000,"c"],[2,1]),
    (["界"*1000,"😀"*750,"c"],[2,1]), (["a"]*9,[8,1]),
    (["a"*6000],[1]), ([],[])], ids=["byte-bound","unicode","count-bound","exact-singleton","empty"])
def test_google_batches_preserve_order_and_evidence(texts, lengths):
    chunks = [chunk(text, i) for i, text in enumerate(texts)]
    result = batcher().batches(chunks)
    assert [len(batch) for batch in result] == lengths
    assert [c for batch in result for c in batch] == chunks
    assert result == batcher().batches(chunks)
    assert all(sum(len(c.text.encode()) for c in batch) <= 6000 for batch in result)


def test_oversized_singleton_fails_and_azure_profile_is_unchanged():
    with pytest.raises(ValueError, match="singleton"):
        batcher().batches([chunk("x"*6001)])
    azure = extraction_batch_config("azure")
    assert (azure.max_tokens, azure.max_chunks, azure.reject_oversized) == (12000,32,False)
    assert batcher("azure").batches([chunk("x"*12001)])


def test_google_source_cap_preserves_unicode_content_hashes_and_provenance():
    text = "start\n" + "界😀"*3000 + "\nend"
    splitter = RepositoryTextSplitter(max_chunk_bytes=extraction_batch_config("google").max_tokens)
    chunks = splitter.split("repo", A, "source.txt", text, ChunkStrategy.WHOLE_FILE)
    assert "".join(c.text for c in chunks) == text
    assert chunks == splitter.split("repo", A, "source.txt", text, ChunkStrategy.WHOLE_FILE)
    assert all(sum(len(c.text.encode()) for c in batch)<=6000 for batch in batcher().batches(chunks))
    for c in chunks:
        assert c == chunk(c.text).create("repo", A, "source.txt", c.chunk_index, c.text, c.strategy, c.metadata)
        assert text[c.metadata["source_char_start"]:c.metadata["source_char_end"]] == c.text


def failed_sync(jobs, **kwargs):
    run_id = enqueue(jobs, trigger="manual_sync", **kwargs)
    owned = jobs.claim(run_id)
    jobs.fail(run_id, owned.owner, "ServerError")
    return run_id


def test_sync_reuses_promoted_full_retry_without_resetting_backoff_or_budget(jobs):
    run_id = failed_sync(jobs)
    before = jobs.get(run_id)
    assert enqueue(jobs, trigger="manual_sync", force_full=False) == run_id
    after = jobs.get(run_id)
    assert (after.attempts,after.next_attempt_at,after.provider_identity) == (before.attempts,before.next_attempt_at,before.provider_identity)
    assert jobs.claim(run_id) is None
    jobs.test_clock[0] += 301
    owned = jobs.claim(run_id)
    assert owned.id == run_id and owned.force_full and owned.attempts == 2
    assert enqueue(jobs, trigger="manual_sync") == run_id
    assert jobs.claim(run_id) is None


@pytest.mark.parametrize("changed", ["commit","trigger","branch","installation"], ids=str)
def test_incompatible_intent_does_not_resume_retry(jobs, changed):
    run_id = failed_sync(jobs)
    if changed in {"branch","installation"}:
        with jobs.sessions.begin() as s:
            run = s.get(IngestionRunModel, run_id)
            if changed == "branch": run.branch = "old"
            else: run.installation_id = "old"
    other = enqueue(jobs, B if changed == "commit" else A,
        trigger="manual" if changed == "trigger" else "manual_sync")
    assert other != run_id
    assert jobs.get(run_id).status == "RETRY" and jobs.get(run_id).attempts == 1


def test_provider_mismatch_fails_closed(jobs):
    run_id = failed_sync(jobs)
    with jobs.sessions.begin() as s:
        s.get(IngestionRunModel, run_id).provider_identity = "incompatible-provider"
    with pytest.raises(ValueError, match="provider changed"):
        enqueue(jobs, trigger="manual_sync")
    assert jobs.get(run_id).attempts == 1


def test_sync_never_grants_a_new_budget_after_exhaustion(jobs):
    run_id = failed_sync(jobs)
    with jobs.sessions.begin() as s:
        run = s.get(IngestionRunModel, run_id)
        run.attempts,run.status = 3,"FAILED"
    with pytest.raises(ValueError, match="retry limit"):
        enqueue(jobs, trigger="manual_sync")
    assert jobs.claim(run_id) is None


def test_last_live_attempt_coalesces_without_preempting_its_lease(jobs):
    run_id = failed_sync(jobs)
    for _ in range(2):
        jobs.test_clock[0] += 301
        owned = jobs.claim(run_id)
        if owned.attempts < 3:
            jobs.fail(run_id, owned.owner, "ServerError")
    assert enqueue(jobs, trigger="manual_sync") == run_id
    assert jobs.get(run_id).status == "INDEXING" and jobs.claim(run_id) is None


async def test_http_sync_reuses_retry_and_skips_live_or_backoff_launches(jobs, monkeypatch):
    from api_services.app.utils import ingestion
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING", "true")
    monkeypatch.setattr(ingestion, "IngestionJobs", lambda _:jobs)
    monkeypatch.setattr(ingestion.time, "time", lambda:jobs.test_clock[0])
    run_id = failed_sync(jobs)
    request = NS(app=NS(state=NS(sqlite_store=NS(session_factory=jobs.sessions), ingestion_service=NS())))
    tasks = NS(add_task=Mock())
    with jobs.sessions() as s:
        repo = s.get(RepositoryModel,"repo")
    assert await ingestion.accept(request,tasks,repo,target=A,trigger="manual_sync") == run_id
    tasks.add_task.assert_not_called()
    jobs.test_clock[0] += 301
    assert await ingestion.accept(request,tasks,repo,target=A,trigger="manual_sync") == run_id
    tasks.add_task.assert_called_once_with(ingestion.dispatch_one,run_id)
    assert jobs.claim(run_id)
    tasks.add_task.reset_mock()
    assert await ingestion.accept(request,tasks,repo,target=A,trigger="manual_sync") == run_id
    tasks.add_task.assert_not_called()


def test_concurrent_sync_has_one_run_and_one_ingestion_owner(jobs):
    run_id = failed_sync(jobs)
    jobs.test_clock[0] += 301
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _:enqueue(jobs, trigger="manual_sync"), range(8)))
        owners = list(pool.map(lambda _:jobs.claim(run_id), range(8)))
    assert set(ids) == {run_id}
    assert sum(owner is not None for owner in owners) == 1
    assert jobs.get(run_id).attempts == 2


def test_ready_baseline_explicit_full_does_not_join_queued_incremental(jobs):
    initial = enqueue(jobs)
    owned = jobs.claim(initial)
    jobs.finish(initial, owned.owner, A)
    incremental = enqueue(jobs, B, trigger="manual_sync")
    full = enqueue(jobs, B, trigger="manual_sync", force_full=True)
    assert full != incremental
    assert not jobs.get(incremental).force_full and jobs.get(full).force_full


@pytest.mark.parametrize("error,expected", [(TimeoutError("private"),"local_deadline"),
    (asyncio.CancelledError(),"cancelled"), (ValueError("private"),"request_failure")])
def test_local_errors_are_classified_without_messages(error, expected):
    assert error_classification(error) == expected


@pytest.mark.parametrize("code,expected", [(504,"provider_deadline"),(429,"provider_rate_limit"),(503,"provider_server")])
async def test_safe_batch_logging_and_provider_error_propagation(code, expected):
    class ProviderError(Exception):
        pass
    error = ProviderError("SECRET SOURCE AND CREDENTIALS")
    error.code = code
    error.response = NS(headers={"x-request-id":"request-123", "authorization":"SECRET"})
    client = NS(aio=NS(models=NS(generate_content=AsyncMock(side_effect=error))))
    llm = VertexLLM(client, config())
    records = []
    handler = logging.Handler()
    handler.emit = lambda record:records.append(json.loads(record.getMessage()))
    logger.addHandler(handler)
    try:
        with pytest.raises(ProviderError):
            await StructuredExtractor(llm).extract([chunk("PRIVATE SOURCE")])
    finally:
        logger.removeHandler(handler)
    assert len(records) == 1
    record = records[0]
    assert record["error_classification"] == expected and record["outcome"] == "error"
    assert record["model"] == "gemini-4-argon" and record["operation"] == "repository_extraction"
    assert record["chunk_count"] == 1 and record["raw_bytes"] == len("PRIVATE SOURCE")
    assert record["rendered_bytes"] > record["raw_bytes"] and record["elapsed_seconds"] >= 0
    assert record["provider_request_id"] == "request-123"
    assert all(text not in json.dumps(record) for text in ["PRIVATE", "SECRET", "private.py", "authorization"])
    assert client.aio.models.generate_content.await_count == 1


async def test_successful_observation_preserves_schema_refs_order_and_cardinality():
    reply = response(json.dumps(dict(entities=[dict(label="Component",name="A",properties=[],source_chunk_refs=["C2"])],relationships=[])))
    client = NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=reply))))
    records = []
    handler = logging.Handler()
    handler.emit = lambda record:records.append(json.loads(record.getMessage()))
    logger.addHandler(handler)
    chunks = [chunk("first",0),chunk("second",1)]
    try:
        result = await StructuredExtractor(VertexLLM(client,config())).extract(chunks)
    finally:
        logger.removeHandler(handler)
    assert [r.chunk_id for r in result] == [c.chunk_id for c in chunks]
    assert not result[0].entities and result[1].entities[0].source_chunk_ids == [chunks[1].chunk_id]
    assert records[0]["outcome"] == "success" and records[0]["provider_request_id"] == "mock-google-id"
    assert client.aio.models.generate_content.call_args.kwargs["config"].http_options.retry_options.attempts == 1
