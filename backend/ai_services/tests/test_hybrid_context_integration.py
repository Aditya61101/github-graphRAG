"""Offline integration: ranks cannot replace original policy or source proof."""

import asyncio
import copy
import threading

import pytest

from ai_services.decision_guard.budget import model_context_json
from ai_services.decision_guard.context import (
    ArchitectureContextBuilder,
    ContextConstructionError,
)
from ai_services.decision_guard.hybrid import HybridRetriever, RetrievalSettings
from ai_services.decision_guard.hybrid.integration import HybridContextRetrieval
from ai_services.tests.test_context_request_budget import fixtures
from ai_services.tests.test_decision_guard_context import (
    BASE,
    HEAD,
    Graph,
    Readiness,
    Source,
    changed,
    changes,
    chunk,
    entity,
)
from ai_services.tests.test_decision_guard_evaluation import TrustedAuthority
from ai_services.tests.test_hybrid_evidence_retrieval import (
    fixture_verifier,
    prepared,
    restamp,
    runner,
)


def tokens(text):
    return (len(text.encode()) + 3) // 4


def codes(ctx):
    return {l.code for l in ctx.coverage.limitations}


def builder(graph, source, index, **options):
    graph.code_chunks = lambda ids, limit: graph.page(
        [r for r in graph.chunks if r["chunk"]["id"] in ids], limit
    )
    config = {
        "index": index,
        "authority": TrustedAuthority(),
        "mode": "ARCHIVED",
        "chunk_verifier": fixture_verifier(graph),
    }
    config.update(options.pop("config", {}))
    return ArchitectureContextBuilder(
        graph,
        source,
        options.pop("readiness", Readiness()),
        options.pop("lock", lambda _: asyncio.Lock()),
        token_counter=tokens,
        hybrid=HybridContextRetrieval(**config),
        **options,
    )


async def test_build_reuses_discovery_original_groups_and_request_projection():
    _, index, graph, source = await prepared(embedding=runner())
    graph.calls.clear()
    source.calls.clear()
    b = builder(graph, source, index)
    ctx = await b.build(changes())
    assert b.last_retrieval_plan and b.last_retrieval_audit["status"] == "VALIDATED", [
        (l.code, l.message) for l in ctx.coverage.limitations
    ]
    assert {r.requirement_id for r in ctx.requirements} == {"governing", "global"}
    assert {r.relationship_type for r in ctx.relationships} == {"CALLS", "READS_FROM"}
    assert len([c for c in graph.calls if c[0] == "snapshot"]) == 1
    assert len([c for c in graph.calls if c[0] == "paths"]) == 1
    assert len([c for c in graph.calls if c[0] == "edges"]) == 1
    assert len(source.calls) == len(set(source.calls))
    assert {e.origin for e in ctx.evidence if e.kind == "CODE"} >= {
        "MERGE_BASE",
        "PR_HEAD",
        "INDEXED_CODE",
    }
    assert "vector_manifest" not in model_context_json(ctx)
    assert "rrf_score" not in model_context_json(ctx)
    assert "HYBRID_CHANNEL_UNAVAILABLE" in codes(ctx)  # no query provider


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "run",
        "base",
        "catalog",
        "scope",
        "statement",
        "targets",
        "mode",
        "size",
        "none",
    ],
)
async def test_stale_manifests_degrade_without_mixing_policy(change):
    _, index, graph, source = await prepared()
    data = index.model_dump(mode="json")
    if change == "generation":
        data["baseline"]["graph_generation"] += 1
    elif change == "run":
        data["baseline"]["ingestion_run_id"] = "another"
    elif change == "base":
        data["baseline"]["indexed_rkg_sha"] = "d" * 40
    elif change == "catalog":
        graph.documents[0]["adr"]["content_hash"] = "e" * 64
    elif change == "scope":
        graph.knowledge[0]["knowledge"]["applicability_json"] = (
            '{"schema_version":"unknown"}'
        )
    elif change == "statement":
        graph.knowledge[0]["knowledge"]["description"] = "Changed original extraction"
    elif change == "targets":
        graph.knowledge[0]["targets"] = []
    elif change == "mode":
        data["mode"] = "LIVE"
    config = {}
    if change == "size":
        config["settings"] = RetrievalSettings(max_index_bytes=50)
    stale = None if change == "none" else restamp(data)
    b = builder(graph, source, stale, config=config)
    ctx = await b.build(changes())
    assert b.last_retrieval_plan is None
    assert "HYBRID_INDEX_OR_PLAN_REJECTED" in codes(ctx)
    assert ctx.deltas and any(e.mapping == "CHANGED_SYMBOL" for e in ctx.entities)


async def test_foreign_repository_is_blocking():
    _, index, graph, source = await prepared()
    # Skip Pydantic construction intentionally: integration revalidates injected snapshots.
    forged = index.model_copy(
        update={
            "baseline": index.baseline.model_copy(update={"repository_id": "foreign"})
        }
    )
    b = builder(graph, source, forged)
    ctx = await b.build(changes())
    assert "HYBRID_INDEX_OR_PLAN_REJECTED" in codes(ctx)
    assert all(e.repository_id == ctx.revision.repository_id for e in ctx.evidence)


@pytest.mark.parametrize(
    "forge",
    ["head", "merge", "candidate", "source", "policy", "relationship", "oversize"],
)
async def test_forged_plans_are_not_selected(forge):
    _, index, graph, source = await prepared()

    class Forged(HybridRetriever):
        async def retrieve(self, discovery, settings):
            plan = await super().retrieve(discovery, settings)
            if forge in {"head", "merge"}:
                return plan.model_copy(
                    update={
                        "revision": plan.revision.model_copy(
                            update={
                                (
                                    "head_sha" if forge == "head" else "merge_base_sha"
                                ): "e" * 40
                            }
                        )
                    }
                )
            if forge == "candidate":
                return plan.model_copy(
                    update={
                        "ranked_adr": (
                            plan.ranked_adr[0].model_copy(
                                update={"source_hash": "f" * 64}
                            ),
                            *plan.ranked_adr[1:],
                        )
                    }
                )
            if forge == "source":
                return plan.model_copy(
                    update={
                        "evidence": (
                            plan.evidence[0].model_copy(
                                update={"source_name": "forged"}
                            ),
                            *plan.evidence[1:],
                        )
                    }
                )
            if forge == "policy":
                return plan.model_copy(
                    update={
                        "requirements": (
                            plan.requirements[0].model_copy(
                                update={"statement": "forged"}
                            ),
                            *plan.requirements[1:],
                        )
                    }
                )
            if forge == "relationship":
                return plan.model_copy(
                    update={
                        "relationships": (
                            plan.relationships[0].model_copy(
                                update={"relationship_type": "FORGED"}
                            ),
                            *plan.relationships[1:],
                        )
                    }
                )
            return plan.model_copy(update={"limitations": plan.limitations * 1000})

    b = builder(
        graph,
        source,
        index,
        config={
            "retriever_factory": Forged,
            "settings": RetrievalSettings(max_index_bytes=100000)
            if forge == "oversize"
            else RetrievalSettings(),
        },
    )
    ctx = await b.build(changes())
    assert b.last_retrieval_plan is None
    assert "HYBRID_INDEX_OR_PLAN_REJECTED" in codes(ctx)
    assert not any(r.relationship_type == "FORGED" for r in ctx.relationships)


@pytest.mark.parametrize("failed", ["primary", "qualification"])
async def test_source_verification_failure_omits_whole_policy_group(failed):
    graph, source = fixtures()
    # Additional qualification under original applicability contract.
    from ai_services.tests.test_decision_guard_scope import (
        enriched,
        repository_metadata,
    )

    meta = repository_metadata()
    meta.applicability.evidence_chunk_ids = ["qualification"]
    rule = enriched(
        meta,
        kid="governing",
        statement="process_request must preserve its response contract",
        targets=[entity()],
    )
    rule["support"] = copy.deepcopy(graph.knowledge[0]["support"])
    graph.knowledge[0] = rule
    qualifier = copy.deepcopy(rule["support"][0])
    qualifier["chunk"].update(
        id="qualification",
        text="Only enabled tasks",
        contentHash=__import__("hashlib").sha256(b"Only enabled tasks").hexdigest(),
    )
    graph.scope_support = [qualifier]
    _, index, graph, source = await prepared(graph, source)
    original = fixture_verifier(graph)
    primary = rule["support"][0]["chunk"]["id"]
    denied = primary if failed == "primary" else "qualification"
    b = builder(
        graph,
        source,
        index,
        config={
            "chunk_verifier": lambda row: row["chunk"]["id"] != denied and original(row)
        },
    )
    ctx = await b.build(changes())
    assert "governing" not in {r.requirement_id for r in ctx.requirements}
    assert "HYBRID_HYDRATION_GROUP_REJECTED" in codes(ctx)


@pytest.mark.parametrize(
    "availability",
    ["absent", "failed_catalog", "disabled_semantic", "no_vectors", "no_index"],
)
async def test_rkg_only_and_partial_channels(availability):
    graph, source = fixtures(adr=availability not in {"absent", "failed_catalog"})
    _, index, graph, source = await prepared(graph, source)
    if availability == "failed_catalog":
        graph.fail_adr = True
    b = builder(
        graph,
        source,
        None if availability == "no_index" else index,
        config={
            "settings": RetrievalSettings(semantic=availability != "disabled_semantic")
        },
    )
    ctx = await b.build(changes())
    assert ctx.deltas and {r.relationship_type for r in ctx.relationships} == {
        "CALLS",
        "READS_FROM",
    }
    assert (ctx.coverage_mode == "RKG_ONLY") == (availability == "absent")
    if availability == "failed_catalog":
        assert not ctx.coverage.adr_catalog_complete
        assert ctx.coverage.adr_retrieval == "DEGRADED"


async def test_sparse_code_lexical_hydration_adds_original_declaration_not_relationship():
    graph, source = Graph(), Source()
    text = "def process_request():\n    return 'old'\n"
    graph.chunks = [
        {
            "path": "other.py",
            "chunk": chunk("other", "other.py", text),
            "entities": [entity("other", "process_request")],
        }
    ]
    source.values[BASE, "other.py"] = source.values[HEAD, "other.py"] = text
    event = changes(changed("new.py"))
    _, index, graph, source = await prepared(graph, source, source_changes=event)
    b = builder(graph, source, index)
    ctx = await b.build(event)
    assert any(
        e.entity_id == "other" and e.paths == ("other.py",) for e in ctx.entities
    )
    assert not ctx.relationships
    assert not ctx.coverage.files[0].mapped


async def test_missing_current_code_and_invalid_hash_are_reported():
    graph, source = Graph(), Source()
    graph.chunks = [
        {
            "path": "other.py",
            "chunk": chunk("other", "other.py"),
            "entities": [entity("other")],
        }
    ]
    source.values[BASE, "other.py"] = source.values[HEAD, "other.py"] = graph.chunks[0][
        "chunk"
    ]["text"]
    event = changes(changed("new.py"))
    _, index, graph, source = await prepared(graph, source, source_changes=event)
    graph.chunks[0]["chunk"]["contentHash"] = "e" * 64
    b = builder(graph, source, index)
    ctx = await b.build(event)
    assert "HYBRID_CODE_SOURCE_REJECTED" in codes(ctx)
    assert not ctx.entities


@pytest.mark.parametrize("mutation", ["catalog", "scope", "readiness"])
async def test_concurrent_mutation_prevents_publication(mutation):
    _, index, graph, source = await prepared()
    readiness = Readiness()

    class Mutating(HybridRetriever):
        async def retrieve(self, discovery, settings):
            plan = await super().retrieve(discovery, settings)
            if mutation == "catalog":
                graph.documents.append(
                    {
                        "adr": {
                            "id": "new",
                            "repository": discovery.revision.repository_id,
                            "content_hash": "f" * 64,
                        }
                    }
                )
            elif mutation == "scope":
                graph.knowledge[0]["knowledge"]["description"] = "Concurrent update"
            else:
                readiness.state = readiness.state.model_copy(
                    update={"graph_generation": 9}
                )
            return plan

    with pytest.raises(ContextConstructionError):
        await builder(
            graph,
            source,
            index,
            readiness=readiness,
            config={"retriever_factory": Mutating},
        ).build(changes())


@pytest.mark.parametrize("phase", ["retrieval", "hydration"])
async def test_cancellation_drains_worker_and_holds_single_lock(phase):
    _, index, graph, source = await prepared()
    entered, release = threading.Event(), threading.Event()
    lock = asyncio.Lock()

    class Blocking(HybridRetriever):
        async def retrieve(self, discovery, settings):
            if phase == "retrieval":
                entered.set()
                await asyncio.to_thread(release.wait, 5)
            return await super().retrieve(discovery, settings)

    original = fixture_verifier(graph)

    def verify(row):
        if phase == "hydration":
            entered.set()
            release.wait(5)
        return original(row)

    b = builder(
        graph,
        source,
        index,
        lock=lambda _: lock,
        config={"retriever_factory": Blocking, "chunk_verifier": verify},
    )
    task = asyncio.create_task(b.build(changes()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert lock.locked() and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()


def test_live_mode_rejects_fixture_verifier():
    with pytest.raises(ValueError):
        HybridContextRetrieval(chunk_verifier=lambda _: True)


async def test_reference_hits_cannot_expand_protected_changed_source_groups():
    graph, source = fixtures()
    text = (
        "# process_request response contract\n" * 100
        + "def unrelated():\n    return process_request()\n"
    )
    graph.chunks.append(
        {
            "path": "extra.py",
            "chunk": chunk("ref", "extra.py", text),
            "entities": [entity(), entity("extra", "unrelated")],
        }
    )
    source.values[BASE, "extra.py"] = source.values[HEAD, "extra.py"] = text
    _, index, graph, source = await prepared(graph, source)
    b = builder(graph, source, index)
    ctx = await b.build(changes())
    primary = next(e for e in ctx.entities if e.entity_id == "process")
    ev = {e.evidence_id: e for e in ctx.evidence}
    assert primary.paths == ("service.py",)
    assert all(ev[eid].path != "extra.py" for eid in primary.evidence_ids)
    assert {r.requirement_id for r in ctx.requirements} >= {"governing"}


async def test_mock_semantic_integration_keeps_original_sources_and_usage():
    embedding = runner()
    _, index, graph, source = await prepared(embedding=embedding)
    b = builder(
        graph,
        source,
        index,
        config={
            "retriever_factory": lambda snapshot, authority: HybridRetriever(
                snapshot, authority=authority, embedder=embedding
            )
        },
    )
    ctx = await b.build(changes())
    assert b.last_retrieval_plan.usage.semantic_provenance == "MOCK"
    assert b.last_retrieval_plan.usage.embedding_requests == 1
    assert index.embedding_requests == 0
    assert all(
        r.policy_status == "UNKNOWN" and r.applicability == "UNASSESSED"
        for r in ctx.requirements
    )


async def test_index_provider_runs_once_under_existing_lock_without_refresh():
    _, index, graph, source = await prepared()
    lock = asyncio.Lock()
    calls = []

    def provider(baseline):
        assert lock.locked()
        calls.append(baseline)
        return index

    b = builder(
        graph, source, None, lock=lambda _: lock, config={"index_provider": provider}
    )
    await b.build(changes())
    assert calls == [index.baseline]
    assert not lock.locked()


async def test_packing_omissions_preserve_complete_qualifier_groups():
    from ai_services.decision_guard.settings import ContextSettings

    _, index, graph, source = await prepared()
    ctx = await builder(
        graph, source, index, settings=ContextSettings(max_context_tokens=2500)
    ).build(changes())
    assert ctx.budget.estimated_tokens <= 2500
    assert all(
        set(r.scope_evidence_ids) <= set(r.evidence_ids) for r in ctx.requirements
    )
    assert "CONTEXT_TOKEN_LIMIT" in codes(ctx)


async def test_independent_files_keep_separate_discovery_and_omission_coverage():
    graph, source = fixtures(adr=False)
    event = changes(changed(), changed("unmapped.py"))
    _, index, graph, source = await prepared(graph, source, source_changes=event)
    ctx = await builder(graph, source, index).build(event)
    files = {f.path: f for f in ctx.coverage.files}
    assert files["service.py"].mapped
    assert not files["unmapped.py"].mapped
    assert any(
        l.code == "DISCOVERY_UNMAPPED_UNIT" and "unmapped.py" in l.paths
        for l in ctx.coverage.limitations
    )


@pytest.mark.parametrize("failure", ["missing", "unavailable", "foreign"])
async def test_authorized_code_hydration_failures_are_explicit(failure):
    graph, source = Graph(), Source()
    graph.chunks = [
        {
            "path": "other.py",
            "chunk": chunk("other", "other.py"),
            "entities": [entity("other")],
        }
    ]
    source.values[BASE, "other.py"] = graph.chunks[0]["chunk"]["text"]
    event = changes(changed("new.py"))
    _, index, graph, source = await prepared(graph, source, source_changes=event)
    b = builder(graph, source, index)
    if failure == "missing":
        graph.code_chunks = lambda ids, limit: graph.page([], limit)
    elif failure == "unavailable":

        def failed(ids, limit):
            raise RuntimeError("Source backend unavailable")

        graph.code_chunks = failed
    else:
        bad = copy.deepcopy(graph.chunks[0])
        bad["chunk"]["repository"] = "foreign"
        graph.code_chunks = lambda ids, limit: graph.page([bad], limit)
        from ai_services.decision_guard.context import RepositoryScopeError

        with pytest.raises(RepositoryScopeError):
            await b.build(event)
        return
    ctx = await b.build(event)
    assert not ctx.entities
    assert codes(ctx) & {
        "HYBRID_CODE_SOURCE_MISSING",
        "HYBRID_CODE_HYDRATION_UNAVAILABLE",
    }


async def test_missing_relationship_support_never_becomes_observed_code():
    graph, source = fixtures(adr=False)
    graph.edges[0]["support"][0]["chunk"]["contentHash"] = "f" * 64
    _, index, graph, source = await prepared(graph, source)
    ctx = await builder(graph, source, index).build(changes())
    assert "UNSUPPORTED_RELATIONSHIP" in codes(ctx)
    assert all(
        r.provenance != "OBSERVED_CODE"
        for r in ctx.relationships
        if r.source_id == "caller"
    )


async def test_unmapped_policy_targets_are_pruned_before_original_policy_support():
    from ai_services.decision_guard.settings import ContextSettings

    graph, source = fixtures()
    # Targets are original intent, not verified code definitions. They remain in
    # requirement.target_links even when their redundant context nodes are pruned.
    graph.chunks[0]["entities"].extend(
        entity(f"intent{i}", f"Intent{i}", source="ADR") for i in range(8)
    )
    graph.knowledge[0]["targets"].extend(
        {
            **entity(f"intent{i}", f"Intent{i}", source="ADR"),
            "relationship": "CONSTRAINS",
        }
        for i in range(8)
    )
    _, index, graph, source = await prepared(graph, source)
    ctx = await builder(
        graph, source, index, settings=ContextSettings(max_context_tokens=6000)
    ).build(changes())
    assert ctx.budget.estimated_tokens <= 6000
    assert all(r.evidence_ids for r in ctx.requirements)
