"""Single authorized Case A query request, followed only by offline comparison."""
import argparse
import asyncio
import json
import os
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import tiktoken
from openai import AsyncAzureOpenAI

from ai_services.runs.prepare_stage_4_1_semantic_retrieval_validation import prepare
from ai_services.runs.benchmark_stage_4_1_hybrid_context import ArchiveContextGraph
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import save, file_hash
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, FixtureReadiness, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.hybrid import EmbeddingLimits, HybridContextRetrieval, HybridRetriever, RetrievalSettings
from ai_services.decision_guard.hybrid.embedding import azure_runner
from ai_services.decision_guard.hybrid.models import SearchIndex
from ai_services.decision_guard.discovery import PreliminaryDiscovery
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.ingestion.pr.models import PullRequestChangeSet

TAG = 'semantic_live_v1'
HASH = '74f6878181133f3ba7aa2ccec7da81dba46eb2131831809bbd47d6c6b245df70'

def out(name):
    return ROOT / f'A_{TAG}_{name}.json'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

class CaptureClient:
    """Records the existing adapter's single request; never retries or adds a call."""
    def __init__(self, client, text):
        self.client, self.text = client, text
        self.embeddings = SimpleNamespace(create=self.create)
        self.calls = 0

    def with_options(self, **options):
        self.client = self.client.with_options(**options)
        return self

    async def create(self, **kwargs):
        assert self.calls == 0 and kwargs['input'] == [self.text]
        self.calls += 1
        save(out('attempt'), {'status': 'ATTEMPT_STARTED', 'query_hash': HASH,
             'authorized_requests': 1, 'retries': 0, 'generative_calls': 0,
             'deployment': kwargs['model'], 'started_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
        started = time.perf_counter()
        try:
            response = await self.client.embeddings.create(**kwargs)
            save(out('raw_response'), response.model_dump(mode='json'))
            save(out('embedding_usage'), {'status': 'RESPONSE_RECEIVED', 'requests': 1,
                 'provider_usage': response.usage.model_dump(mode='json') if response.usage else None,
                 'estimated_input_tokens': 687, 'output_tokens': 0,
                 'latency_seconds': time.perf_counter() - started,
                 'returned_model': response.model, 'query_hash': HASH})
            return response
        except BaseException as error:
            # Never serialize exception messages that could contain endpoint/credentials.
            save(out('embedding_failure'), {'status': 'FAILED_OR_UNCERTAIN_NO_RETRY',
                 'requests_attempted': 1, 'error_type': type(error).__name__,
                 'latency_seconds': time.perf_counter() - started})
            raise

async def run(compare_only=False):
    if not compare_only:
        assert not out('attempt').exists() and not out('raw_response').exists(), 'Request already attempted; never repeat'
        await prepare(TAG + '_precheck')
    protected = {p.name: file_hash(p) for p in ROOT.glob('*.json')}
    if not compare_only:
        save(out('protected'), protected)
    else:
        protected = read(out('protected'))
    precheck = read(ROOT / f'A_{TAG}_precheck_preflight.json')
    assert precheck['query_hash'] == HASH and precheck['query_input_tokens'] == 687
    index = SearchIndex.model_validate_json((ROOT / 'A_step2b_chunk_reuse_index.json').read_text(encoding='utf-8'))
    discovery = PreliminaryDiscovery.model_validate_json((ROOT / 'A_step2a_complete_discovery.json').read_text(encoding='utf-8'))
    queries = [q for q in discovery.queries if q.channel == 'ADR_SEMANTIC']
    assert len(queries) == 1 and digest(queries[0].text) == HASH
    query = queries[0]
    manifest = read(ROOT / 'manifest.json')
    archive_text = (ROOT / 'claimiq_graph_snapshot.json').read_text(encoding='utf-8')
    assert digest(archive_text) == manifest['snapshot_hash']
    event = PullRequestChangeSet.model_validate_json((ROOT / 'A_changes.json').read_text(encoding='utf-8'))
    source, _ = source_for(event.repository_id, Path(next(c for c in manifest['cases'] if c['id'] == 'A')['source_root']))
    authority = UploadAuthority(ReadOnlyStore('data/decisionguard.db'), 'data/adrs')
    limits = EmbeddingLimits(max_requests=1, batch_size=1, concurrency=1, retries=0, authorized_billable=True)
    if compare_only:
        # No client constructed, no provider path. Only the already persisted response.
        from ai_services.decision_guard.hybrid import EmbeddingRunner
        class Forbidden:
            dimensions = 3072
            async def embed(self, texts):
                raise AssertionError('Offline comparison cannot make requests')
        runner = EmbeddingRunner(Forbidden(), index.vector_manifest.identity, limits)
        values = read(out('raw_response'))['data'][0]['embedding']
        runner.cache[query.text] = tuple(values)
    else:
        config = precheck['configuration']
        client = AsyncAzureOpenAI(api_key=os.environ['AZURE_OPENAI_API_KEY'], azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'], api_version=config['api_version'], max_retries=0, timeout=limits.timeout_seconds)
        capture = CaptureClient(client, query.text)
        runner = azure_runner(capture, config['deployment'], limits=limits)
        encoding = tiktoken.get_encoding('cl100k_base')
        runner.counter = lambda t: len(encoding.encode(t, disallowed_special=()))
        try:
            await runner.embed([query.text])
        finally:
            await client.close()
        assert capture.calls == runner.requests == 1 and runner.identity == index.vector_manifest.identity
    # All subsequent retrieval uses the validated single-query cache, not Azure.
    assert len(runner.cache[query.text]) == 3072
    model_encoding = tiktoken.get_encoding('o200k_base')
    counter = ModelTokenCounter('o200k_base:deployment-estimate', lambda t: len(model_encoding.encode(t, disallowed_special=())))
    evaluation = EvaluationSettings.model_validate(manifest['settings'])
    metrics, plans, contexts = {}, {}, {}
    started_comparison = time.perf_counter()
    for name, semantic in [('without_semantic', False), ('with_semantic', True)]:
        sizing = ContextRequestBudget(source, authority, settings=evaluation, token_counter=counter)
        def factory(snapshot, *, authority):
            return HybridRetriever(snapshot, authority=authority, embedder=runner if semantic else None)
        builder = ArchitectureContextBuilder(ArchiveContextGraph(json.loads(archive_text)), source,
             FixtureReadiness(event.repository_id, event.base_sha, manifest['snapshot_hash'], event.github_repository_id, event.base_branch), lambda _: asyncio.Lock(),
             settings=ContextSettings(max_decisions=4, max_constraints=8), token_counter=counter, request_budget=sizing,
             hybrid=HybridContextRetrieval(index=index, authority=authority, mode='ARCHIVED', settings=RetrievalSettings(semantic=semantic), retriever_factory=factory))
        started = time.perf_counter()
        context = validate_context(await builder.build(event))
        latency = time.perf_counter() - started
        plan = builder.last_retrieval_plan
        assert plan is not None
        if semantic:
            assert len(plan.semantic_chunks) == 21 and plan.usage.semantic_provenance == 'VERIFIED_PROVIDER'
        assert all(sizing.fits(p, 'ASSESS') for p in sizing.last_packets)
        assert all(r.status == 'APPROVED' for r in authority.receipts(context))
        inputs = [sizing.count(p, 'ASSESS') for p in sizing.last_packets]
        metrics[name] = {'requirements': [r.requirement_id for r in context.requirements],
             'entities': [e.entity_id for e in context.entities], 'relationships': [r.model_dump(mode='json') for r in context.relationships],
             'code_candidates': len(plan.ranked_code), 'adr_candidates': len(plan.ranked_adr),
             'context_tokens': counter(model_context_json(context)), 'units': sum(len(p.units) for p in sizing.last_packets),
             'packets': len(inputs), 'packet_input_tokens': inputs,
             'assessment_reservation': sum(inputs) + len(inputs)*evaluation.max_output_tokens,
             'context_latency_seconds': latency, 'cached_retrieval_latency_seconds': plan.usage.latency_seconds,
             'coverage': context.coverage.model_dump(mode='json'),
             'plan_omissions': dict(Counter(l.code for l in plan.limitations))}
        plans[name], contexts[name] = plan, context
        save(out(name + '_plan'), plan.model_dump(mode='json'))
        save(out(name + '_context'), context.model_dump(mode='json'))
        save(out(name + '_packets'), [p.model_dump(mode='json') for p in sizing.last_packets])
        save(out(name + '_audit'), builder.last_retrieval_audit)
    # Labels applied only after all selections are complete.
    original = ArchitectureContext.model_validate_json((ROOT/'A_context.json').read_text(encoding='utf-8'))
    expected = original.requirements[0].requirement_id
    evidence = {e.evidence_id:e for e in index.evidence}
    chunk_metadata = {c.chunk_id:c for c in index.adr_chunks}
    ranked = []
    for h in plans['with_semantic'].semantic_chunks:
        c = chunk_metadata[h.chunk_id]
        ranked.append({**h.model_dump(mode='json'), 'adr_id': c.adr_id,
            'section': evidence[c.evidence_id].section, 'mapped_requirement_count': len(h.requirement_ids),
            'source_verified': True, 'vector_verified': True})
    for name, plan in plans.items():
        candidate = next((c for c in plan.ranked_adr if c.original_id == expected), None)
        rule = next((r for r in contexts[name].requirements if r.requirement_id == expected), None)
        ev = {e.evidence_id:e for e in contexts[name].evidence}
        metrics[name].update(governing_rank=candidate.rank if candidate else None,
            labelled_recall={str(k):int(bool(candidate and candidate.rank<=k)) for k in (1,5,10)},
            governing_sources=[{'chunk_id':ev[e].chunk_id, 'hash':ev[e].content_hash, 'qualification':e in rule.scope_evidence_ids} for e in rule.evidence_ids] if rule else [])
    before = {c.original_id for c in plans['without_semantic'].ranked_adr}
    after = {c.original_id for c in plans['with_semantic'].ranked_adr}
    result = {'status':'COMPLETE_REAL_SINGLE_QUERY_RETRIEVAL', 'query_hash':HASH,
         'index_id':index.index_id, 'embedding_identity':runner.identity.model_dump(mode='json'),
         'retrieval_settings':asdict(RetrievalSettings()), 'embedding_limits':asdict(limits),
         'embedding_usage':read(out('embedding_usage')), 'ranked_chunks':ranked,
         'governing_requirement_id':expected,
         'governing_semantic_hit':next((h for h in ranked if expected in h['requirement_ids']),None),
         'semantic_enabled_new_candidates':sorted(after-before), 'semantic_enabled_lost_candidates':sorted(before-after),
         'configurations':metrics, 'offline_comparison_seconds':time.perf_counter()-started_comparison,
         'fixture_readiness_only':True, 'generative_calls':0, 'document_reembedding_calls':0}
    assert all(file_hash(ROOT/n)==h for n,h in protected.items())
    save(out('comparison'), result)
    save(out('integrity'), {'historical_files_unchanged':{n:True for n in protected}, 'historical_ledger_sha256':file_hash(ROOT/'aggregate_usage.json'), 'provider_requests':1})
    print(json.dumps({'status': result['status'], 'usage': result['embedding_usage'], 'configurations': {name: {key: metric[key] for key in ('governing_rank', 'context_tokens', 'units', 'packets', 'assessment_reservation')} for name, metric in metrics.items()}}, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-only', action='store_true')
    asyncio.run(run(parser.parse_args().compare_only))
