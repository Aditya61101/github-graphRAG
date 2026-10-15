"""Independent coherent-unit capacity and explicit omission regressions."""

import asyncio
import copy
import json

import pytest

from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, model_adapter
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    RequestBudget,
    make_request,
)
from ai_services.decision_guard.evaluation.models import EvidenceBundle
from ai_services.decision_guard.evaluation.wire import (
    compact_context_records,
    expand_source_texts,
    provider_request_json,
)
from ai_services.tests.test_assessment_packet_planning import (
    Capacity,
    context,
    manifest,
)
from ai_services.tests.test_context_request_budget import fixtures
from ai_services.tests.test_decision_guard_context import Readiness, changes
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    evaluate,
    tokens,
)


@pytest.mark.parametrize("extra", [1, 30, 120])
async def test_prompt_growth_splits_feasible_units_instead_of_omitting(
    extra, monkeypatch
):
    ctx, units = await context()
    units = units[:2]
    budget = RequestBudget(EvaluationSettings(max_input_tokens=100000), tokens)
    pair = budget.count(make_request(ctx, units, EvidenceBundle(), ()), "ASSESS")
    singles = [
        budget.count(make_request(ctx, (u,), EvidenceBundle(), ()), "ASSESS")
        for u in units
    ]
    assert max(singles) < pair
    monkeypatch.setattr(
        model_adapter,
        "ASSESS_PROMPT",
        model_adapter.ASSESS_PROMPT + "\n" + ("additional contract " * extra),
    )
    probe = RequestBudget(EvaluationSettings(max_input_tokens=100000), tokens)
    # Choose the exact boundary where the original pair fits and both new
    # singles fit, while the newly enlarged pair does not.
    limit = max(
        pair,
        *(
            probe.count(make_request(ctx, (u,), EvidenceBundle(), ()), "ASSESS")
            for u in units
        ),
    )
    measured = RequestBudget(EvaluationSettings(max_input_tokens=limit), tokens)
    assert (
        measured.count(make_request(ctx, units, EvidenceBundle(), ()), "ASSESS") > limit
    )
    packets, omitted = measured.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted and len(packets) == 2
    assert sum(len(p.units) for p in packets) == 2
    assert all(measured.fits(p, "ASSESS") for p in packets)


async def test_oversized_unit_cannot_flush_or_displace_other_units():
    ctx, units = await context()
    budget = Capacity(units, (6, 11, 4, 4))
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert [u.unit_id for u in omitted] == [units[1].unit_id]
    assert {u.unit_id for p in packets for u in p.units} == {
        units[i].unit_id for i in (0, 2, 3)
    }
    assert budget.last_unit_measurements[1].input_tokens == 11
    assert all(budget.fits(p, "ASSESS") for p in packets)


async def test_individual_feasibility_includes_exact_shared_request_overhead():
    ctx, units = await context()

    class Shared(Capacity):
        def count(self, request, phase):
            self.calls += 1
            return 6 + len(request.units) * 2

    budget = Shared(units, (0,) * 4)
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted and len(packets) == 2
    assert all(m.input_tokens == 8 for m in budget.last_unit_measurements)
    assert all(len(p.units) == 2 and budget.count(p, "ASSESS") == 10 for p in packets)


async def test_intrinsic_adr_omissions_preserve_rkg_and_never_return_clean():
    graph, source = fixtures()
    ctx0 = await ArchitectureContextBuilder(
        graph, source, Readiness(), lambda _: asyncio.Lock(), token_counter=tokens
    ).build(changes())
    sizing = ContextRequestBudget(
        source,
        TrustedAuthority(),
        settings=EvaluationSettings(max_input_tokens=100000),
        token_counter=tokens,
    )
    await asyncio.to_thread(sizing, ctx0)
    costs = sizing.last_unit_measurements
    rkg = next(m.input_tokens for m in costs if m.rule_id == "rkg-semantic-v1")
    adr = min(m.input_tokens for m in costs if m.rule_id != "rkg-semantic-v1")
    assert rkg < adr
    settings = EvaluationSettings(max_input_tokens=(rkg + adr) // 2, retries=0)
    sizing = ContextRequestBudget(
        source, TrustedAuthority(), settings=settings, token_counter=tokens
    )
    ctx = await ArchitectureContextBuilder(
        graph,
        source,
        Readiness(),
        lambda _: asyncio.Lock(),
        token_counter=tokens,
        request_budget=sizing,
    ).build(changes())
    assert len(ctx.requirements) == 2 and len(ctx.budget.request_omitted_units) == 2
    assert all(
        m.rule_id != "rkg-semantic-v1" and m.input_tokens > settings.max_input_tokens
        for m in ctx.budget.request_omitted_units
    )
    model = ScriptedModel()
    result = await evaluate(ctx, source, model, settings=settings)
    assert (
        len(result.plan) == 3
        and result.coverage.omitted == 2
        and result.coverage.assessed == 1
    )
    assert result.outcome == "REVIEW_REQUIRED"
    assert len(model.requests) == 1 and all(
        u.channel == "RKG" for u in model.requests[0].units
    )


def test_optional_metadata_compaction_preserves_all_qualifications_and_edges():
    link = {
        "entity_id": "entity",
        "entity_type": "Service",
        "name": "worker",
        "origin": "CODE",
    }
    applicability = {
        "conditions": ["enabled"],
        "exceptions": ["signed override"],
        "qualifiers": ["active"],
        "uncertainty": ["partial discovery"],
        "evidence_chunk_ids": ["original"],
        "rationale": "Generated explanatory prose",
        "targets": [
            {
                "name": "worker",
                "description": "Generated target explanation",
                "label": None,
                "evidence_chunk_ids": ["original"],
            }
        ],
    }
    records = {
        "entities": [],
        "relationships": [],
        "requirements": [
            {
                "requirement_id": "r",
                "statement": "Original extracted obligation",
                "evidence_ids": ["original"],
                "scope_evidence_ids": ["qualification"],
                "target_links": [
                    {**link, "relationship": "AFFECTS"},
                    {**link, "relationship": "CONSTRAINS"},
                ],
                "declared_applicability_json": json.dumps(
                    {"applicability": applicability, "context_truncated": True}
                ),
            }
        ],
    }
    original = copy.deepcopy(records)
    payload = {"context_records_json": json.dumps(records)}
    compact_context_records(payload)
    rule = json.loads(payload["context_records_json"])["requirements"][0]
    assert records == original
    assert rule["target_links"] == [
        {**link, "relationships": ["AFFECTS", "CONSTRAINS"]}
    ]
    for key in (
        "conditions",
        "exceptions",
        "qualifiers",
        "uncertainty",
        "evidence_chunk_ids",
    ):
        assert rule["scope_hints"]["applicability"][key] == applicability[key]
    assert rule["scope_hints"]["context_truncated"] is True
    assert rule["evidence_ids"] == ["original"] and rule["scope_evidence_ids"] == [
        "qualification"
    ]
    assert "rationale" not in rule["scope_hints"]["applicability"]


async def test_repacked_sources_and_unit_obligations_remain_independent():
    ctx, units = await context()
    budget = Capacity(units, (6, 6, 4, 4))
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted
    for packet in packets:
        expanded = expand_source_texts(
            json.loads(provider_request_json(packet, "ASSESS"))
        )
        assert expanded["evidence"] == [
            e.model_dump(mode="json") for e in packet.evidence
        ]
        for u in packet.units:
            original = make_request(
                ctx,
                (next(x for x in units if x.unit_id == u.unit_id),),
                EvidenceBundle(),
                (),
            )
            assert u == original.units[0]
    first = manifest(packets)
    again, _ = budget.pack(ctx, list(reversed(units)), EvidenceBundle(), ())
    assert manifest(again) == first
    assert [p.model_dump_json() for p in again] == [
        p.model_dump_json() for p in packets
    ]
