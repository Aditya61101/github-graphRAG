"""Repository-independent structural/BM25/RRF and mocked-vector regressions."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_services.decision_guard import ArchitectureContextBuilder
from ai_services.decision_guard.context import ContextConstructionError
from ai_services.decision_guard.hybrid import (
    DerivedIndexBuilder,
    EmbeddingIdentity,
    EmbeddingLimits,
    EmbeddingRunner,
    HybridRetriever,
    IndexSettings,
    RetrievalSettings,
)
from ai_services.decision_guard.hybrid.lexical import BM25, tokenize
from ai_services.decision_guard.hybrid.models import (
    EvidencePlan,
    SearchIndex,
    VectorCompatibilityManifest,
    confirmed_legacy_manifest,
    fingerprint,
)
from ai_services.decision_guard.hybrid.store import Neo4jSearchIndexStore
from ai_services.decision_guard.models import digest
from ai_services.tests.test_context_request_budget import fixtures
from ai_services.tests.test_decision_guard_context import (
    BASE,
    HEAD,
    NEW,
    OLD,
    REPO,
    Graph,
    Readiness,
    Source,
    changed,
    changes,
    chunk,
    entity,
    requirement,
)
from ai_services.tests.test_decision_guard_evaluation import TrustedAuthority
from ai_services.tests.test_decision_guard_scope import (
    attach,
    attach_community,
    community_metadata,
    enriched,
)


def inventory_graph(graph):
    graph.code_inventory = lambda limit: graph.page(
        sorted(graph.chunks, key=lambda r: (r["path"], r["chunk"]["id"])), limit
    )

    def adr_rows():
        rows = {}
        for item in (
            [s for r in graph.knowledge for s in r.get("support", ())]
            + graph.scope_support
            + getattr(graph, "vector_chunks", [])
        ):
            if not item.get("chunk") or not item.get("adr"):
                continue
            cid = item["chunk"]["id"]
            rows[cid] = {
                "chunk": copy.deepcopy(item["chunk"]),
                "adr": copy.deepcopy(item["adr"]),
                "requirements": [],
            }
        for rule in graph.knowledge:
            for item in rule.get("support", ()):
                if item.get("chunk"):
                    rows[item["chunk"]["id"]]["requirements"].append(
                        {
                            "id": rule["knowledge"]["id"],
                            "repository": REPO,
                            "adr_id": item["adr"]["id"],
                        }
                    )
        return sorted(rows.values(), key=lambda r: r["chunk"]["id"])

    graph.adr_vector_rows = adr_rows
    graph.adr_chunks = lambda ids, limit: graph.page(
        [r for r in adr_rows() if r["adr"]["id"] in ids], limit
    )
    return graph


def mock_manifest(identity=None):
    return VectorCompatibilityManifest(
        configuration_id="fixture-chunk-vectors-v1",
        identity=identity or runner().identity,
        confirmation="MOCK_FIXTURE",
        confirmation_source="Deterministic existing chunk vectors, not provider performance",
    )


def fixture_verifier(graph):
    originals = {
        r["chunk"]["id"]: copy.deepcopy(r["chunk"]) for r in graph.adr_vector_rows()
    }
    return lambda row: (
        row["chunk"].get("text") == originals[row["chunk"]["id"]].get("text")
        and row["chunk"].get("contentHash")
        == originals[row["chunk"]["id"]].get("contentHash")
    )


class MockEmbedder:
    dimensions = 2

    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [
            [1.0, 0.0] if "PR_HEAD" in t or "durability" in t.casefold() else [0.0, 1.0]
            for t in texts
        ]


def runner(provider=None, **bounds):
    return EmbeddingRunner(
        provider or MockEmbedder(),
        EmbeddingIdentity(
            dimensions=2, revision="independent-vector-test-v1", provenance="MOCK"
        ),
        EmbeddingLimits(**bounds),
    )


async def prepared(
    graph=None, source=None, *, embedding=None, source_changes=None, index_settings=None
):
    if graph is None:
        graph, source = fixtures()
    graph = inventory_graph(graph)
    if embedding:
        for item in (
            [s for r in graph.knowledge for s in r.get("support", ())]
            + graph.scope_support
            + getattr(graph, "vector_chunks", [])
        ):
            text = item["chunk"]["text"]
            item["chunk"].setdefault(
                "embedding",
                [1.0, 0.0] if "durability" in text.casefold() else [0.0, 1.0],
            )
    source = source or Source()
    discovery = await ArchitectureContextBuilder(
        graph, source, Readiness(), lambda _: asyncio.Lock()
    ).discover(source_changes or changes())
    index = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        settings=index_settings,
        compatibility_manifest=mock_manifest(embedding.identity if embedding else None),
        chunk_verifier=fixture_verifier(graph),
    ).refresh(discovery.baseline)
    return discovery, index, graph, source


def restamp(data):
    data["index_id"] = fingerprint(
        {
            k: v
            for k, v in data.items()
            if k not in {"index_id", "embedding_requests", "embeddings_reused"}
        }
    )
    return SearchIndex.model_validate_json(json.dumps(data))


async def test_structural_retrieval_preserves_directed_original_relationship_groups():
    discovery, index, _, _ = await prepared()
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(lexical=False, scope=False, semantic=False)
    )
    assert plan.ranked_code and plan.ranked_adr
    assert {r.relationship_type for r in plan.relationships} == {"CALLS", "READS_FROM"}
    assert plan.relationships == tuple(
        sorted(discovery.relationships, key=lambda r: r.relationship_id)
    )
    ev = {e.evidence_id: e for e in plan.evidence}
    assert all(set(r.evidence_ids) <= ev.keys() for r in plan.relationships)
    assert all(c.hydration_required for c in (*plan.ranked_code, *plan.ranked_adr))
    assert all(c.channels[0].channel == "CODE_RKG" for c in plan.ranked_code)


async def test_code_bm25_exact_identifiers_and_sparse_graph_source_inventory():
    graph, source = Graph(), Source()
    graph.chunks = [
        {
            "path": "other.py",
            "chunk": chunk(
                "other", "other.py", "def processPayment():\n    return 'settled'\n"
            ),
            "entities": [entity("payment", "processPayment")],
        },
    ]
    source.values[BASE, "other.py"] = graph.chunks[0]["chunk"]["text"]
    source.values[HEAD, "other.py"] = graph.chunks[0]["chunk"]["text"]
    event = changes(
        changed(
            "new.ts",
            "function processPayment() { return false; }",
            "function processPayment() { return true; }",
        )
    )
    discovery, index, _, _ = await prepared(graph, source, source_changes=event)
    assert not discovery.relationships and not discovery.seeds[0].baseline_entity_ids
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(structural=False, scope=False, semantic=False)
    )
    assert plan.ranked_code[0].original_id == "other"
    engine = BM25([u for u in index.units if u.category == "CODE"])
    assert engine.search("processPayment") and engine.search("process_payment")
    assert {"processpayment", "process", "payment"} <= set(tokenize("processPayment"))


async def test_adr_bm25_discovers_unlinked_cross_cutting_source_without_applicability_verdict():
    discovery, index, _, _ = await prepared()
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(structural=False, scope=False, semantic=False)
    )
    assert "global" in {c.original_id for c in plan.ranked_adr}
    rule = next(r for r in plan.requirements if r.requirement_id == "global")
    assert rule.applicability == "UNASSESSED" and rule.policy_status == "UNKNOWN"
    assert rule.evidence_ids


async def test_global_scope_and_unknown_scope_remain_candidates_without_lexical_match():
    graph, source = fixtures()
    metadata = json.loads(
        __import__(
            "ai_services.ingestion.adr.applicability",
            fromlist=["ApplicabilityMetadata"],
        )
        .ApplicabilityMetadata()
        .to_json()
    )
    metadata["applicability"]["scopes"] = ["repository"]
    graph.knowledge[1]["knowledge"]["applicability_json"] = json.dumps(metadata)
    graph.knowledge[1]["knowledge"]["description"] = (
        "Durability precedes outbound dispatch."
    )
    graph.knowledge.append(
        {
            **requirement(
                "unknown", targets=[], statement="Exceptions require approval."
            ),
            "support": graph.knowledge[1]["support"],
        }
    )
    discovery, index, _, _ = await prepared(graph, source)
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(structural=False, lexical=False, semantic=False)
    )
    assert "global" in {c.original_id for c in plan.ranked_adr}
    global_hit = next(c for c in plan.ranked_adr if c.original_id == "global")
    assert "REPOSITORY_SCOPE" in global_hit.channels[0].hints
    assert any(
        "FALLBACK" in h
        for c in plan.ranked_adr
        for entry in c.channels
        for h in entry.hints
    )


async def test_mocked_semantic_channel_handles_different_wording_and_unrelated_lexical_noise():
    graph, source = fixtures()
    graph.knowledge[0]["knowledge"]["description"] = (
        "Durability must precede outbound dispatch."
    )
    graph.knowledge[1]["knowledge"]["description"] = (
        "The decorative status label controls the display palette."
    )
    for rule, text, index in (
        (graph.knowledge[0], "Durability must precede outbound dispatch.", 0),
        (
            graph.knowledge[1],
            "The decorative status label controls the display palette.",
            1,
        ),
    ):
        support = copy.deepcopy(rule["support"][0])
        support["chunk"].update(
            id=f"adr:adr-1:chunk:{index}", text=text, contentHash=digest(text)
        )
        rule["support"] = [support]
    provider = runner()
    discovery, index, _, _ = await prepared(graph, source, embedding=provider)
    plan = await HybridRetriever(index, embedder=provider).retrieve(
        discovery, RetrievalSettings(structural=False, lexical=False, scope=False)
    )
    assert plan.ranked_adr[0].original_id == "governing"
    assert plan.usage.semantic_provenance == "MOCK"
    assert plan.usage.embedding_requests == 1
    assert plan.ranked_adr[0].channels[0].raw_score == pytest.approx(1.0)
    assert next(c for c in plan.ranked_adr if c.original_id == "global").channels[
        0
    ].raw_score == pytest.approx(0.0)


async def test_rrf_deduplicates_channel_identity_and_never_adds_raw_scores():
    discovery, index, _, _ = await prepared(embedding=runner())
    plan = await HybridRetriever(index, embedder=runner()).retrieve(discovery)
    assert len({c.canonical_id for c in plan.ranked_adr}) == len(plan.ranked_adr)
    candidate = next(c for c in plan.ranked_adr if c.original_id == "governing")
    best = {}
    for hit in candidate.channels:
        best[hit.channel] = min(best.get(hit.channel, 10000), hit.position)
    assert candidate.rrf_score == pytest.approx(
        sum(1 / (60 + p) for p in best.values())
    )
    assert {"ADR_BM25", "ADR_ASSOCIATION", "ADR_SCOPE", "ADR_SEMANTIC"} <= set(best)
    again = await HybridRetriever(index, embedder=runner()).retrieve(discovery)
    assert [(c.canonical_id, c.rrf_score) for c in again.ranked_adr] == [
        (c.canonical_id, c.rrf_score) for c in plan.ranked_adr
    ]


@pytest.mark.parametrize("adrs,failed", [(False, False), (True, False), (True, True)])
async def test_no_adrs_no_candidates_and_failed_metadata_keep_code_retrieval(
    adrs, failed
):
    graph, source = fixtures(adr=adrs, failed=failed)
    if adrs and not failed:
        graph.knowledge = []
    discovery, index, _, _ = await prepared(graph, source)
    provider = runner()
    plan = await HybridRetriever(index, embedder=provider).retrieve(discovery)
    assert plan.ranked_code and not plan.ranked_adr
    assert plan.adr_status == (
        "DEGRADED" if failed else "AVAILABLE" if adrs else "ABSENT"
    )
    assert provider.requests == 0
    if failed:
        assert any(l.code == "ADR_CATALOG_UNAVAILABLE" for l in plan.limitations)


async def test_semantic_failure_preserves_structural_lexical_and_scope():
    embedder = runner()
    discovery, index, _, _ = await prepared(embedding=embedder)

    class Broken(MockEmbedder):
        async def embed(self, texts):
            raise RuntimeError("offline")

    plan = await HybridRetriever(index, embedder=runner(Broken())).retrieve(discovery)
    assert plan.ranked_code and plan.ranked_adr
    assert (
        next(c for c in plan.channels if c.channel == "ADR_SEMANTIC").status == "FAILED"
    )
    assert any(l.code == "SEMANTIC_CHANNEL_FAILED" for l in plan.limitations)


async def test_lexical_failure_does_not_erase_structural_or_scope(monkeypatch):
    discovery, index, _, _ = await prepared()
    monkeypatch.setattr(
        BM25, "search", lambda *args: (_ for _ in ()).throw(RuntimeError())
    )
    plan = await HybridRetriever(index).retrieve(discovery)
    assert plan.ranked_code and plan.ranked_adr
    assert next(c for c in plan.channels if c.channel == "CODE_BM25").status == "FAILED"


async def test_stale_generation_and_corrupted_index_fall_back_to_verified_discovery():
    discovery, index, _, _ = await prepared()
    bad = index.model_copy(
        update={"baseline": index.baseline.model_copy(update={"graph_generation": 999})}
    )
    plan = await HybridRetriever(bad).retrieve(discovery)
    assert plan.ranked_code and not plan.ranked_adr
    assert plan.index_id is None and plan.adr_status == "DEGRADED"
    assert any(
        l.code == "RETRIEVAL_INDEX_STALE_OR_UNAVAILABLE" for l in plan.limitations
    )
    raw = index.model_dump(mode="json")
    raw["units"][0]["lexical_tokens"].append("tampered")
    with pytest.raises(ValidationError):
        SearchIndex.model_validate_json(json.dumps(raw))


async def test_foreign_repository_index_cannot_leak_candidates():
    discovery, index, _, _ = await prepared()
    raw = index.model_dump(mode="json")
    raw["baseline"]["repository_id"] = "foreign"
    with pytest.raises(ValidationError):
        restamp(raw)
    plan = await HybridRetriever(
        index.model_copy(
            update={
                "baseline": index.baseline.model_copy(
                    update={"repository_id": "foreign"}
                )
            }
        )
    ).retrieve(discovery)
    assert plan.index_id is None and all(e.repository_id == REPO for e in plan.evidence)


async def test_invalid_source_hash_is_rejected_using_shared_context_validator():
    graph, source = fixtures()
    graph.chunks[0]["chunk"]["contentHash"] = digest("wrong")
    _discovery, index, _, _ = await prepared(graph, source)
    assert "chunk-service" not in {u.original_id for u in index.units}
    assert any(l.code == "CODE_INDEX_SOURCE_REJECTED" for l in index.limitations)


async def test_equivalent_embeddings_are_reused_and_changed_sources_reindex():
    embedder = runner()
    discovery, first, graph, source = await prepared(embedding=embedder)
    calls = embedder.requests
    builder = DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        compatibility_manifest=mock_manifest(embedder.identity),
        chunk_verifier=fixture_verifier(graph),
    )
    second = await builder.refresh(discovery.baseline, first)
    assert second.embeddings_reused == len(first.adr_chunks)
    assert embedder.requests == calls and first.index_id == second.index_id
    graph.knowledge[0]["knowledge"]["description"] += (
        " Explicit exceptions need review."
    )
    third = await builder.refresh(discovery.baseline, second)
    assert third.embeddings_reused == second.embeddings_reused
    assert third.adr_chunks == second.adr_chunks
    assert embedder.requests == calls == 0 and third.index_id != second.index_id


async def test_embedding_model_dimensions_and_source_versions_are_not_interchangeable():
    with pytest.raises(ValueError, match="dimension"):
        EmbeddingRunner(
            MockEmbedder(), EmbeddingIdentity(dimensions=3, provenance="MOCK")
        )
    discovery, index, _, _ = await prepared(embedding=runner())
    other = EmbeddingRunner(
        MockEmbedder(),
        EmbeddingIdentity(
            model="different-model",
            dimensions=2,
            revision="different",
            provenance="MOCK",
        ),
    )
    plan = await HybridRetriever(index, embedder=other).retrieve(discovery)
    assert other.requests == 0
    assert (
        next(c for c in plan.channels if c.channel == "ADR_SEMANTIC").status
        == "UNAVAILABLE"
    )
    raw = index.model_dump(mode="json")
    adr = raw["adr_chunks"][0]
    adr["vector"]["key"] = "stale"
    with pytest.raises(ValidationError, match="Stale"):
        restamp(raw)


async def test_billable_embeddings_require_approval_and_code_never_enters_provider():
    provider = MockEmbedder()
    blocked = EmbeddingRunner(provider, EmbeddingIdentity(dimensions=2))
    with pytest.raises(ValueError, match="approval"):
        await blocked.embed(["private input"])
    assert not provider.calls
    allowed = runner(provider)
    discovery, index, _, _ = await prepared(embedding=allowed)
    assert all(
        "def process_request" not in text for batch in provider.calls for text in batch
    )
    before = allowed.requests
    await HybridRetriever(index, embedder=allowed).retrieve(
        discovery, RetrievalSettings(semantic=False)
    )
    assert allowed.requests == before


async def test_fair_queries_and_candidate_budgets_cover_independent_files():
    graph, source = Graph(), Source()
    graph.chunks = []
    files = []
    for i in range(4):
        path = f"service{i}.py"
        before = OLD.replace("process_request", f"handle{i}")
        after = NEW.replace("process_request", f"handle{i}")
        files.append(changed(path, before, after))
        graph.chunks.append(
            {
                "path": path,
                "chunk": chunk(str(i), path, before),
                "entities": [entity(str(i), f"handle{i}")],
            }
        )
        source.values[BASE, path], source.values[HEAD, path] = before, after
    discovery, index, _, _ = await prepared(
        graph, source, source_changes=changes(*files)
    )
    plan = await HybridRetriever(index).retrieve(
        discovery,
        RetrievalSettings(
            max_queries=4,
            max_code_candidates=4,
            max_candidates=4,
            structural=False,
            semantic=False,
            scope=False,
        ),
    )
    assert len(plan.ranked_code) == 4
    assert {sid for c in plan.ranked_code for sid in c.seed_ids} == {
        s.seed_id for s in discovery.seeds
    }
    assert any(l.code == "RETRIEVAL_QUERY_OMITTED" for l in plan.limitations)


async def test_large_catalog_has_bounded_plan_and_compact_diagnostics():
    graph, source = fixtures(count=250)
    discovery, index, _, _ = await prepared(graph, source)
    plan = await HybridRetriever(index).retrieve(
        discovery,
        RetrievalSettings(
            results_per_query=200,
            max_adr_candidates=12,
            max_code_candidates=6,
            max_candidates=18,
        ),
    )
    assert len(plan.ranked_adr) <= 12 and len(plan.ranked_code) <= 6
    assert len(plan.limitations) > 100
    assert len(json.dumps(plan.compact_coverage)) < 15000
    assert plan.compact_coverage["partial"] and plan.compact_coverage["diagnostic_hash"]
    assert any(l.code == "RANKED_CANDIDATE_OMITTED" for l in plan.limitations)
    assert EvidencePlan.model_validate_json(plan.model_dump_json()) == plan


async def test_index_inventory_caps_and_missing_sources_report_partial_coverage():
    discovery, index, _, _ = await prepared(
        index_settings=IndexSettings(max_code_units=1, max_adr_units=1)
    )
    assert any(l.code == "CODE_INDEX_TRUNCATED" for l in index.limitations)
    assert any(l.code == "ADR_REQUIREMENTS_TRUNCATED" for l in index.limitations)
    plan = await HybridRetriever(index).retrieve(discovery)
    assert plan.compact_coverage["partial"]


async def test_source_identity_preservation_and_approval_revocation():
    discovery, index, _, _ = await prepared()
    plan = await HybridRetriever(index).retrieve(discovery)
    original = {e.evidence_id: e for e in (*discovery.evidence, *index.evidence)}
    assert all(e == original[e.evidence_id] for e in plan.evidence)

    class Revoked:
        def receipts(self, _):
            return ()

    degraded = await HybridRetriever(index, authority=Revoked()).retrieve(discovery)
    assert degraded.ranked_code and not degraded.ranked_adr
    assert degraded.adr_status == "DEGRADED"


async def test_live_refresh_requires_real_readiness_lease_and_rejects_race():
    discovery, _, graph, source = await prepared()
    with pytest.raises(ContextConstructionError, match="requires"):
        await DerivedIndexBuilder(graph, source).refresh(discovery.baseline)
    readiness = Readiness()
    calls = 0

    async def snapshot(_):
        nonlocal calls
        calls += 1
        return (
            readiness.state
            if calls == 1
            else readiness.state.model_copy(update={"graph_generation": 8})
        )

    readiness.snapshot = snapshot
    with pytest.raises(ContextConstructionError, match="changed"):
        await DerivedIndexBuilder(
            graph,
            source,
            TrustedAuthority(),
            readiness=readiness,
            repository_lock=lambda _: asyncio.Lock(),
        ).refresh(discovery.baseline)


async def test_provider_retries_requests_cache_and_nonfinite_bounds_are_enforced():
    class Flaky(MockEmbedder):
        async def embed(self, texts):
            if not self.calls:
                self.calls.append(tuple(texts))
                raise RuntimeError("once")
            return await super().embed(texts)

    e = runner(Flaky(), retries=1, max_requests=2)
    vectors = await e.embed(["durability"])
    assert e.requests == 2 and vectors == [(1.0, 0.0)]
    assert await e.embed(["durability"]) == vectors and e.requests == 2
    with pytest.raises(ValueError, match="budget"):
        await e.embed(["next"])
    with pytest.raises(ValueError, match="length"):
        await runner(max_text_chars=1).embed(["long"])


async def test_neo4j_derived_store_is_scoped_immutable_and_does_not_write_original_labels():
    _, index, _, _ = await prepared()

    class Driver:
        def __init__(self):
            self.calls = []

        def execute_query(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return SimpleNamespace(
                records=[
                    {
                        "repository": kwargs.get("repo"),
                        "key": kwargs.get("key"),
                        "state": "READY",
                    }
                ]
            )

    driver = Driver()
    store = Neo4jSearchIndexStore(driver, "neo4j")
    store.initialize()
    store.save(index)
    assert all("DGRetrievalIndex" in q for q, _ in driver.calls)
    assert not any(
        "SET c." in q or "SET adr." in q or "DELETE" in q for q, _ in driver.calls
    )
    assert driver.calls[-1][1]["repo"] == REPO


async def test_store_load_filters_identity_and_volume_before_payload_read():
    _, index, _, _ = await prepared()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin_transaction(self, **kwargs):
            self.timeout = kwargs["timeout"]
            return self

        def run(self, query, **kwargs):
            self.query, self.params = query, kwargs
            return [{"payload": index.model_dump_json()}]

    session = Session()
    driver = SimpleNamespace(session=lambda **_: session)
    assert (
        Neo4jSearchIndexStore(driver, "neo4j").load(index.index_id, index.baseline)
        == index
    )
    assert (
        "repository: $repo" in session.query
        and "generation: $generation" in session.query
    )
    assert "n.payload_bytes <= $max_bytes" in session.query
    assert session.params["repo"] == REPO and session.timeout == 30
    with pytest.raises(ValueError, match="mismatch"):
        Neo4jSearchIndexStore(driver, "neo4j").load("different", index.baseline)


@pytest.mark.parametrize("values", [[0.0, 0.0], [float("nan"), 1.0], [1.0]])
async def test_invalid_provider_vectors_are_rejected(values):
    class Invalid(MockEmbedder):
        async def embed(self, texts):
            return [values for _ in texts]

    with pytest.raises(ValueError):
        await runner(Invalid()).embed(["one"])


async def test_embedding_concurrency_and_timeout_are_bounded():
    class Slow(MockEmbedder):
        active = peak = 0

        async def embed(self, texts):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.02)
                return await super().embed(texts)
            finally:
                self.active -= 1

    provider = Slow()
    bounded = runner(provider, batch_size=1, concurrency=2)
    assert len(await bounded.embed(["a", "b", "c", "d"])) == 4
    assert provider.peak == 2 and provider.active == 0
    with pytest.raises(TimeoutError):
        await runner(provider, timeout_seconds=0.001).embed(["later"])
    assert provider.active == 0


async def test_expired_retrieval_deadline_reports_truncation_not_absence():
    discovery, index, _, _ = await prepared()
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(deadline_seconds=1e-12)
    )
    assert not plan.ranked_code and not plan.ranked_adr
    assert any(l.code == "RETRIEVAL_DEADLINE" for l in plan.limitations)
    assert plan.adr_status == "AVAILABLE" and plan.compact_coverage["partial"]


async def test_total_budget_reserves_code_and_reports_policy_exclusions():
    discovery, index, _, _ = await prepared()
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(max_candidates=2)
    )
    assert len(plan.ranked_code) == len(plan.ranked_adr) == 1
    assert any(l.code == "RANKED_CANDIDATE_OMITTED" for l in plan.limitations)


async def test_changed_scope_metadata_invalidates_derived_identity():
    discovery, first, graph, source = await prepared(embedding=runner())
    graph.knowledge[0]["knowledge"]["applicability_json"] = '{"unexpected":true}'
    second = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        compatibility_manifest=mock_manifest(),
        chunk_verifier=fixture_verifier(graph),
    ).refresh(discovery.baseline, first)
    assert second.index_id != first.index_id
    assert second.embeddings_reused == first.embeddings_reused
    plan = await HybridRetriever(second).retrieve(discovery)
    assert plan.source_freshness == "SNAPSHOT_ONLY"
    assert all(r.applicability == "UNASSESSED" for r in plan.requirements)


async def test_many_hunks_in_first_file_do_not_displace_an_independent_file():
    graph, source = Graph(), Source()
    graph.chunks, files = [], []
    for path, names in (
        ("alpha.py", ["dispatch_a", "dispatch_b", "dispatch_c"]),
        ("zeta.py", ["invoice_total"]),
    ):
        before = "\n" * 20
        for name in names:
            text = f"def {name}():\n    return 0\n"
            before += text + "\n" * 20
            graph.chunks.append(
                {
                    "path": path,
                    "chunk": chunk(name, path, text),
                    "entities": [entity(name, name)],
                }
            )
        after = before.replace("return 0", "return 1")
        files.append(changed(path, before, after))
        source.values[BASE, path], source.values[HEAD, path] = before, after
    discovery, index, _, _ = await prepared(
        graph, source, source_changes=changes(*files)
    )
    assert len([s for s in discovery.seeds if s.path == "alpha.py"]) == 3
    plan = await HybridRetriever(index).retrieve(
        discovery,
        RetrievalSettings(max_candidates=2, max_code_candidates=2, results_per_file=1),
    )
    ev = {e.evidence_id: e for e in plan.evidence}
    assert {ev[c.evidence_ids[0]].path for c in plan.ranked_code} == {
        "alpha.py",
        "zeta.py",
    }
    assert any(l.code == "RANKED_CANDIDATE_OMITTED" for l in plan.limitations)


async def test_source_read_quota_excludes_unverified_chunks_with_explicit_coverage():
    discovery, index, _, _ = await prepared(
        index_settings=IndexSettings(max_source_reads=1)
    )
    assert len([u for u in index.units if u.category == "CODE"]) == 1
    assert any(l.code == "INDEXED_SOURCE_UNAVAILABLE" for l in index.limitations)
    plan = await HybridRetriever(index).retrieve(discovery)
    assert plan.ranked_code and plan.compact_coverage["partial"]


@pytest.mark.parametrize(
    "governs,basis,missing,expected",
    [
        (True, "explicit", False, True),
        (False, "explicit", False, False),
        (True, "uncertain", False, False),
        (True, "explicit", True, False),
    ],
)
async def test_community_discovery_reuses_semantic_scope_and_original_support_checks(
    governs, basis, missing, expected
):
    graph, source = Graph(), Source()
    attach(graph, [enriched(community_metadata(governs=governs, basis=basis))])
    attach_community(graph)
    if missing:
        graph.scope_support = []
    discovery, index, _, _ = await prepared(graph, source)
    plan = await HybridRetriever(index).retrieve(
        discovery, RetrievalSettings(structural=False, lexical=False, semantic=False)
    )
    assert plan.ranked_adr
    hints = {h for c in plan.ranked_adr for entry in c.channels for h in entry.hints}
    assert ("COMMUNITY_SCOPE" in hints) is expected
    if missing:
        assert any(l.code == "INDEX_SCOPE_EVIDENCE_MISSING" for l in plan.limitations)
        assert plan.compact_coverage["partial"]
    assert all(r.applicability == "UNASSESSED" for r in plan.requirements)


async def test_failed_code_inventory_keeps_discovery_structural_and_adr_channels():
    discovery, _, graph, source = await prepared()
    graph.code_inventory = lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
    index = await DerivedIndexBuilder(
        graph, source, TrustedAuthority(), mode="ARCHIVED"
    ).refresh(discovery.baseline)
    assert not any(u.category == "CODE" for u in index.units)
    plan = await HybridRetriever(index).retrieve(discovery)
    assert plan.ranked_code and plan.ranked_adr
    assert any(l.code == "CODE_INDEX_UNAVAILABLE" for l in plan.limitations)
    assert (
        next(c for c in plan.channels if c.channel == "CODE_BM25").status
        == "UNAVAILABLE"
    )


async def test_index_deadline_stops_further_source_reads_and_reports_omissions():
    graph, source = Graph(), Source()
    graph.chunks = []
    for i in range(20):
        path, text = f"module{i}.py", f"def handler{i}():\n    return {i}\n"
        graph.chunks.append(
            {
                "path": path,
                "chunk": chunk(str(i), path, text),
                "entities": [entity(str(i), f"handler{i}")],
            }
        )
        source.values[BASE, path] = text
    graph = inventory_graph(graph)
    read = source.read_blob

    def delayed(*args):
        time.sleep(0.22)
        return read(*args)

    source.read_blob = delayed
    index = await DerivedIndexBuilder(
        graph, source, mode="ARCHIVED", settings=IndexSettings(deadline_seconds=1)
    ).refresh(Readiness().state)
    assert len(source.calls) <= 5
    assert len([u for u in index.units if u.category == "CODE"]) < 20
    assert any(
        l.code == "INDEX_DEADLINE" and len(l.item_ids) >= 10 for l in index.limitations
    )
    assert index.adr_status == "DEGRADED" and not index.catalog_complete


async def test_shared_chunk_siblings_share_semantic_rank_and_no_requirement_vectors():
    provider = runner()
    discovery, index, _, _ = await prepared(embedding=provider)
    assert provider.requests == index.embedding_requests == 0
    assert len(index.adr_chunks) == 1
    assert all(
        "vector" not in u.model_dump() and "semantic_text" not in u.model_dump()
        for u in index.units
    )
    plan = await HybridRetriever(index, embedder=provider).retrieve(
        discovery, RetrievalSettings(structural=False, lexical=False, scope=False)
    )
    assert len(plan.ranked_adr) == 2
    hits = [c.channels[0] for c in plan.ranked_adr]
    assert hits[0].position == hits[1].position == 1
    assert (
        hits[0].source_chunk_id
        == hits[1].source_chunk_id
        == index.adr_chunks[0].chunk_id
    )
    assert (
        len(plan.semantic_chunks) == 1
        and plan.semantic_chunks[0].metadata_basis == "MOCK_FIXTURE"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_model", "wrong-model"),
        ("embedding_dimensions", 3),
        ("embedding_provider", "google"),
        ("embedding_revision", "other-space"),
        ("embedding_text_hash", "stale"),
    ],
)
async def test_conflicting_observed_vector_metadata_rejected_without_other_channel_changes(
    field, value
):
    discovery, first, graph, source = await prepared(embedding=runner())
    graph.knowledge[0]["support"][0]["chunk"][field] = value
    index = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        compatibility_manifest=mock_manifest(),
        chunk_verifier=fixture_verifier(graph),
    ).refresh(discovery.baseline)
    assert not index.adr_chunks
    assert [(u.original_id, u.lexical_tokens) for u in index.units] == [
        (u.original_id, u.lexical_tokens) for u in first.units
    ]
    assert any(l.code == "ADR_CHUNK_VECTOR_REJECTED" for l in index.limitations)


async def test_legacy_manifest_confirmation_is_not_claimed_as_neo4j_model_metadata():
    manifest = confirmed_legacy_manifest()
    assert (
        manifest.identity.model == "text-embedding-3-large"
        and manifest.identity.dimensions == 3072
    )
    assert (
        manifest.confirmation == "USER_CONFIRMED_LEGACY"
        and not manifest.neo4j_model_metadata_claimed
    )
    graph, source = fixtures()
    inventory_graph(graph)
    for rule in graph.knowledge:
        rule["support"][0]["chunk"]["embedding"] = [1.0] + [0.0] * 3071
    index = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        chunk_verifier=fixture_verifier(graph),
    ).refresh(Readiness().state)
    chunk = index.adr_chunks[0]
    assert chunk.metadata_basis == "LEGACY_CONFIRMED"
    assert chunk.verified_metadata_fields == () and chunk.observed_metadata_json == "{}"
    c = graph.knowledge[0]["support"][0]["chunk"]
    c.update(
        embedding_model=manifest.identity.model,
        embedding_provider=manifest.identity.provider,
        embedding_dimensions=3072,
        embedding_revision=manifest.identity.revision,
        embedding_text_hash=c["contentHash"],
    )
    verified = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        chunk_verifier=fixture_verifier(graph),
    ).refresh(Readiness().state)
    assert verified.adr_chunks[0].metadata_basis == "NEO4J_METADATA_VERIFIED"
    assert len(verified.adr_chunks[0].verified_metadata_fields) == 5
    assert verified.index_id != index.index_id


async def test_context_only_chunk_preserved_without_inventing_requirements_and_fanout_audited():
    graph, source = fixtures()
    context = copy.deepcopy(graph.knowledge[0]["support"][0])
    context["chunk"].update(
        id="context-only",
        text="Durability exceptions require explicit approval.",
        contentHash=digest("Durability exceptions require explicit approval."),
    )
    graph.vector_chunks = [context]
    discovery, index, _, _ = await prepared(graph, source, embedding=runner())
    assert len(index.adr_chunks) == 2
    plan = await HybridRetriever(index, embedder=runner()).retrieve(
        discovery,
        RetrievalSettings(
            structural=False,
            lexical=False,
            scope=False,
            semantic_requirements_per_query=1,
        ),
    )
    assert len(plan.ranked_adr) == 1
    assert any(l.code == "SEMANTIC_REQUIREMENT_OMITTED" for l in plan.limitations)
    context_hit = next(h for h in plan.semantic_chunks if h.chunk_id == "context-only")
    assert context_hit.requirement_ids == ()
    assert any(e.chunk_id == "context-only" for e in plan.evidence)
    limited = await HybridRetriever(index, embedder=runner()).retrieve(
        discovery, RetrievalSettings(semantic_chunks_per_query=1)
    )
    assert any(l.code == "SEMANTIC_CHUNK_OMITTED" for l in limited.limitations)


async def test_qualification_evidence_preserved_even_if_its_chunk_is_not_a_semantic_hit():
    graph, source = fixtures()
    primary = graph.knowledge[0]["support"][0]
    primary["chunk"]["embedding"] = [1.0, 0.0]
    qualifier = copy.deepcopy(primary)
    qualifier["chunk"].update(
        id="qualifying-evidence",
        text="The condition applies to active tasks only.",
        contentHash=digest("The condition applies to active tasks only."),
        embedding=[0.0, 1.0],
    )
    graph.scope_support = [qualifier]
    from ai_services.ingestion.adr.applicability import ApplicabilityMetadata

    meta = ApplicabilityMetadata()
    meta.applicability.evidence_chunk_ids = ["qualifying-evidence"]
    meta.applicability.qualifiers = ["Active tasks only"]
    graph.knowledge[0]["knowledge"]["applicability_json"] = meta.to_json()
    discovery, index, _, _ = await prepared(graph, source, embedding=runner())
    plan = await HybridRetriever(index, embedder=runner()).retrieve(
        discovery,
        RetrievalSettings(
            structural=False, lexical=False, scope=False, semantic_chunks_per_query=1
        ),
    )
    assert plan.semantic_chunks[0].chunk_id != "qualifying-evidence"
    rule = next(r for r in plan.requirements if r.requirement_id == "governing")
    assert rule.scope_evidence_ids
    assert any(
        e.chunk_id == "qualifying-evidence" and e.evidence_id in rule.evidence_ids
        for e in plan.evidence
    )


async def test_forged_chunk_rank_or_source_in_plan_is_rejected():
    discovery, index, _, _ = await prepared(embedding=runner())
    plan = await HybridRetriever(index, embedder=runner()).retrieve(discovery)
    data = plan.model_dump(mode="json")
    entry = next(
        h
        for c in data["ranked_adr"]
        for h in c["channels"]
        if h["channel"] == "ADR_SEMANTIC"
    )
    entry["position"] += 1
    with pytest.raises(ValidationError, match="projection"):
        EvidencePlan.model_validate_json(json.dumps(data))


async def test_original_upload_verifier_failure_rejects_vector_not_lexical_policy():
    discovery, _, graph, source = await prepared(embedding=runner())
    index = await DerivedIndexBuilder(
        graph,
        source,
        TrustedAuthority(),
        mode="ARCHIVED",
        compatibility_manifest=mock_manifest(),
        chunk_verifier=lambda _: False,
    ).refresh(discovery.baseline)
    assert not index.adr_chunks and any(u.category == "ADR" for u in index.units)
    plan = await HybridRetriever(index, embedder=runner()).retrieve(discovery)
    assert plan.ranked_code and plan.ranked_adr and plan.usage.embedding_requests == 0


async def test_provider_independent_space_identity_prevents_azure_gemini_vector_mixing():
    discovery, index, _, _ = await prepared(embedding=runner())
    gemini = EmbeddingRunner(
        MockEmbedder(),
        EmbeddingIdentity(
            provider="google",
            model="gemini-embedding",
            dimensions=2,
            revision="gemini-space-v1",
            provenance="MOCK",
        ),
    )
    plan = await HybridRetriever(index, embedder=gemini).retrieve(discovery)
    assert gemini.requests == 0 and plan.usage.semantic_provenance == "UNMEASURED"
