"""Independent deterministic evidence-group and serialized-request budget regressions."""

import asyncio
import json

import pytest

from ai_services.decision_guard.budget import coverage_summary, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    RequestBudget,
)
from ai_services.decision_guard.evaluation.models import EvaluationSettings
from ai_services.decision_guard.evaluation.wire import (
    compact_context_records,
    expand_source_texts,
    intern_source_texts,
    provider_request_json,
)
from ai_services.decision_guard.models import EvidenceSnapshot, Limitation, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.tests.test_decision_guard_context import (
    BASE,
    HEAD,
    OLD,
    Graph,
    Readiness,
    Source,
    changes,
    chunk,
    entity,
    requirement,
)
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    evaluate,
    tokens,
)


def fixtures(*, count=0, adr=True, failed=False):
    graph, source = Graph(), Source()
    task = entity("task", "Task")
    definition = "class Task:\n    enabled = True\n    done = False\n"
    caller = entity("caller", "invoke")
    caller_text = "def invoke():\n    return process_request()\n"
    for node, path, text in (
        (task, "domain.py", definition),
        (caller, "caller.py", caller_text),
    ):
        graph.chunks.append(
            {"path": path, "chunk": chunk(node["id"], path, text), "entities": [node]}
        )
        source.values[BASE, path] = text
        source.values[HEAD, path] = text

    def edge(src, dst, path, text, kind):
        return {
            "source": src,
            "target": dst,
            "assertion": {
                "id": src["id"] + "|" + kind + "|" + dst["id"],
                "repository": src["repository"],
                "relationshipType": kind,
                "rationale": "Original directed relationship",
            },
            "support": [
                {"chunk": chunk(src["id"] + dst["id"], path, text), "is_adr": False}
            ],
        }

    graph.edges = [
        edge(caller, entity(), "caller.py", caller_text, "CALLS"),
        edge(entity(), task, "service.py", OLD, "READS_FROM"),
    ]
    # Many peripheral source-backed rows exercise whole edge/source-group pruning.
    for i in range(count):
        text = "# " + "peripheral " * 100 + "\n" + OLD
        path = f"extra/{i}.py"
        source.values[BASE, path] = text
        source.values[HEAD, path] = text
        graph.edges.append(
            edge(entity(), entity(f"extra{i}", f"other{i}"), path, text, "USES")
        )
    if adr:
        policy = "Accepted decision: All service functions preserve the documented response contract. Exceptions require explicit approval."
        support = {
            "adr": {
                "id": "adr-1",
                "repository": entity()["repository"],
                "content_hash": digest(policy),
                "source_name": "policy.md",
            },
            "chunk": {
                "id": "adr:policy",
                "repository": entity()["repository"],
                "adr_id": "adr-1",
                "text": policy,
                "contentHash": digest(policy),
                "section": "Decision",
            },
        }
        primary = requirement(
            "governing",
            kind="CONSTRAINT",
            statement="process_request must preserve its response contract",
        )
        cross = requirement("global", kind="CONSTRAINT", targets=[], statement=policy)
        graph.knowledge = [
            {**primary, "support": [support]},
            {**cross, "support": [support]},
        ]
        for i in range(count):
            graph.knowledge.append(
                {
                    **requirement(
                        f"noise{i}",
                        kind="CONSTRAINT",
                        targets=[task],
                        statement=f"Another component has an independent rule {i}",
                    ),
                    "support": [support],
                }
            )
        graph.documents = [{"adr": support["adr"]}]
    graph.fail_adr = failed
    return graph, source


async def build(
    graph, source, *, context_limit=24000, input_limit=16000, authority=None
):
    settings = EvaluationSettings(max_input_tokens=input_limit)
    sizing = ContextRequestBudget(
        source, authority, settings=settings, token_counter=tokens
    )
    builder = ArchitectureContextBuilder(
        graph,
        source,
        Readiness(),
        lambda _: asyncio.Lock(),
        ContextSettings(max_context_tokens=context_limit, max_catalog_items=600),
        token_counter=tokens,
        request_budget=sizing,
    )
    ctx = await builder.build(changes())
    return ctx, sizing, settings


def test_repeated_diagnostics_are_bounded_and_audit_hash_covers_every_detail():
    details = tuple(
        Limitation(
            code="OMITTED",
            message="Very long diagnostic " * 100,
            item_ids=(str(i),),
            paths=(f"src/{i}.py",),
        )
        for i in range(1000)
    )
    summary = coverage_summary(details, "repo-one")
    assert (
        summary["total_details"] == 1000 and summary["categories"][0]["count"] == 1000
    )
    assert len(summary["categories"][0]["example_item_ids"]) == 2
    assert tokens(json.dumps(summary)) < 400
    assert (
        coverage_summary(details[:-1], "repo-one")["diagnostic_hash"]
        != summary["diagnostic_hash"]
    )
    assert (
        coverage_summary(details, "repo-two")["diagnostic_hash"]
        != summary["diagnostic_hash"]
    )


async def test_large_catalog_preserves_primary_cross_cutting_policy_and_code_groups():
    graph, source = fixtures(count=350)
    ctx, sizing, _ = await build(graph, source, authority=TrustedAuthority())
    assert {"governing", "global"} <= {r.requirement_id for r in ctx.requirements}
    assert any(r.relationship_type == "CALLS" for r in ctx.relationships)
    assert any(e.path == "domain.py" for e in ctx.evidence)
    assert ctx.budget.accounting == "MODEL_PROJECTION"
    assert ctx.budget.audit_tokens > ctx.budget.estimated_tokens
    assert ctx.budget.estimated_tokens <= 24000
    assert ctx.budget.request_input_tokens <= ctx.budget.request_input_limit == 16000
    assert len(ctx.coverage.limitations) > 300
    assert all(
        RequestBudget(sizing.settings, tokens).fits(p, "ASSESS")
        for p in sizing.last_packets
    )
    for req in sizing.last_packets:
        wire = json.loads(provider_request_json(req, "ASSESS"))
        assert "limitations" not in wire and len(wire["coverage"]["categories"]) <= 12
        assert wire["coverage"]["partial"] and wire["coverage"]["diagnostic_hash"]
        assert tokens(json.dumps(wire["coverage"])) < 2000
        assert len(req.limitations) > 300  # Canonical request remains a complete audit.
    assert tokens(model_context_json(ctx)) == ctx.budget.estimated_tokens


@pytest.mark.parametrize("adr,failed", [(False, False), (True, False), (True, True)])
async def test_rkg_groups_and_budgeting_do_not_depend_on_adr_presence(adr, failed):
    graph, source = fixtures(adr=adr, failed=failed)
    if adr and not failed:
        # ADRs exist, but the graph returned no relevant requirement candidates.
        graph.knowledge = []
    ctx, sizing, settings = await build(graph, source)
    assert not ctx.requirements
    assert ctx.relationships and any(e.path == "caller.py" for e in ctx.evidence)
    expected = "DEGRADED" if failed else "AVAILABLE" if adr else "ABSENT"
    assert ctx.coverage.adr_retrieval == expected
    assert all(p.adr_retrieval == expected for p in sizing.last_packets)
    model = ScriptedModel({("RKG", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, settings=settings)
    assert (
        result.outcome == "CONFLICTS_FOUND"
        and result.findings[0].kind == "RKG_REGRESSION"
    )
    if failed:
        assert result.coverage.scope == "PARTIAL"
        assert any(l.code == "ADR_RETRIEVAL_FAILED" for l in result.limitations)


async def test_minimum_required_source_cannot_fit_and_dispatch_stops_without_calls():
    graph, source = fixtures(adr=False)
    ctx, sizing, settings = await build(graph, source, input_limit=1000)
    # Source availability is not assessment feasibility. Preserve the delta and
    # its proof evidence, and expose its independently measured request omission.
    assert ctx.deltas and ctx.coverage.files[0].mapped
    assert ctx.coverage.files[0].status == "TEXT"
    assert ctx.budget.request_omitted_units
    assert all(u.input_tokens > 1000 for u in ctx.budget.request_omitted_units)
    assert not sizing.last_packets
    model = ScriptedModel()
    result = await evaluate(ctx, source, model, settings=settings)
    assert not model.requests and result.outcome == "NOT_EVALUATED"
    assert result.coverage.scope == "UNASSESSABLE"
    assert result.coverage.omitted == len(result.plan) == 1
    assert result.units[0].status == "BUDGET_OMITTED"


async def test_same_packet_capacity_is_used_by_context_preparation_and_dispatch():
    graph, source = fixtures(count=20, adr=False)
    ctx, sizing, settings = await build(graph, source, input_limit=11000)
    assert ctx.deltas and sizing.last_packets
    model = ScriptedModel()
    await evaluate(ctx, source, model, settings=settings)
    assert len(model.requests) == len(sizing.last_packets)
    for actual, planned in zip(model.requests, sizing.last_packets, strict=True):
        assert provider_request_json(actual, "ASSESS") == provider_request_json(
            planned, "ASSESS"
        )
        assert (
            sizing.count(actual, "ASSESS") == sizing.count(planned, "ASSESS") <= 11000
        )


async def test_group_selection_does_not_rewrite_evidence_or_citation_identity():
    graph, source = fixtures(count=60)
    ctx, sizing, _ = await build(
        graph, source, context_limit=8000, authority=TrustedAuthority()
    )
    assert ctx.deltas and ctx.requirements
    from ai_services.decision_guard.evaluation.evidence import resolve_quote
    from ai_services.decision_guard.evaluation.models import QuoteRef

    for req in sizing.last_packets:
        ev = {e.evidence_id: e for e in req.evidence}
        for e in req.evidence:
            assert digest(e.excerpt) == e.excerpt_hash
            if e.kind == "CODE" and e.location and e.excerpt:
                citation = resolve_quote(
                    QuoteRef(evidence_id=e.evidence_id, quote=e.excerpt[:1000]), ev
                )
                assert citation.location.path == e.path
                assert citation.start_char == 0 and citation.end_char == min(
                    1000, len(e.excerpt)
                )


def source_projection():
    text = "def worker():\n    return '☃'\n"
    evidence = []
    for identifier, origin, excerpt in (
        ("base", "MERGE_BASE", text),
        ("head", "PR_HEAD", text),
        ("indexed", "INDEXED_CODE", "return '☃'"),
    ):
        evidence.append(
            EvidenceSnapshot(
                evidence_id=identifier,
                repository_id="repo-independent",
                kind="CODE",
                origin=origin,
                path="worker.py",
                commit_sha=HEAD if origin == "PR_HEAD" else BASE,
                excerpt=excerpt,
                excerpt_hash=digest(excerpt),
                completeness="COMPLETE" if identifier != "indexed" else "EXCERPT",
                placement="UNLOCATED",
            ).model_dump(mode="json")
        )
    return {"evidence": evidence}


def test_shared_source_text_is_lossless_without_sharing_revision_authority():
    original = source_projection()
    projected = json.loads(json.dumps(original))
    intern_source_texts(projected)
    assert len(projected["source_texts"]) == 1
    assert expand_source_texts(projected) == original
    assert {e["origin"] for e in projected["evidence"]} == {
        "MERGE_BASE",
        "PR_HEAD",
        "INDEXED_CODE",
    }
    assert {e["evidence_id"] for e in projected["evidence"]} == {
        "base",
        "head",
        "indexed",
    }


@pytest.mark.parametrize("damage", ["hash", "bounds", "unknown_text", "excerpt_hash"])
def test_source_projection_rejects_corruption(damage):
    projected = source_projection()
    intern_source_texts(projected)
    if damage == "hash":
        projected["source_texts"][0]["text"] += "modified"
    elif damage == "bounds":
        projected["evidence"][0]["excerpt_ref"]["end_char"] = 10000
    elif damage == "unknown_text":
        projected["evidence"][0]["excerpt_ref"]["text_id"] = "missing"
    else:
        projected["evidence"][0]["excerpt_hash"] = digest("modified")
    with pytest.raises(ValueError):
        expand_source_texts(projected)


def test_compact_scope_keeps_qualifiers_exceptions_and_original_evidence_links():
    metadata = {
        "applicability": {
            "conditions": ["Associated enabled tasks only"],
            "exceptions": ["Explicit administrative override"],
            "evidence_chunk_ids": ["policy-original"],
        },
        "entity_ids": ["Task"],
        "community_ids": [],
        "unresolved_targets": ["external consumer"],
        "context_truncated": True,
    }
    record = {
        "requirement_id": "rule",
        "evidence_ids": ["policy-original"],
        "scope_status": "VALID",
        "declared_applicability_json": json.dumps(metadata),
        "enrichment_fingerprint": "redundant",
        "relevance_score": 100,
    }
    packet = {
        "context_records_json": json.dumps(
            {"entities": [], "relationships": [], "requirements": [record]}
        )
    }
    compact_context_records(packet)
    compacted = json.loads(packet["context_records_json"])["requirements"][0]
    assert compacted["scope_hints"] == metadata
    assert compacted["scope_status"] == "VALID"
    assert compacted["evidence_ids"] == ["policy-original"]
    assert "enrichment_fingerprint" not in compacted


def test_request_capacity_reserves_output_even_when_input_limit_is_larger():
    settings = EvaluationSettings(
        max_input_tokens=20000, model_window=22000, max_output_tokens=5000
    )
    assert RequestBudget(settings, tokens).input_limit == 17000
