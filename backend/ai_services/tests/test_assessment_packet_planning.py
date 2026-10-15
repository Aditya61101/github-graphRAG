"""Independent packet capacity, backfilling and original-request integrity."""

import asyncio
import copy
import itertools

import pytest

from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation.engine import (
    RequestBudget,
    build_units,
    make_request,
)
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    EvaluationInputError,
    EvaluationSettings,
    EvidenceBundle,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.wire import (
    expand_source_texts,
    provider_request_json,
)
from ai_services.tests.test_context_request_budget import fixtures
from ai_services.tests.test_decision_guard_context import Readiness, changes


async def context():
    graph, source = fixtures()
    template = copy.deepcopy(graph.knowledge[0])
    graph.knowledge = []
    for i in range(4):
        row = copy.deepcopy(template)
        row["knowledge"]["id"] = f"rule-{i}"
        if i == 3:
            row["support"][0]["chunk"]["id"] = "alternate-original-support"
        graph.knowledge.append(row)
    ctx = await ArchitectureContextBuilder(
        graph, source, Readiness(), lambda _: asyncio.Lock()
    ).build(changes())
    units, _ = build_units(ctx, 20)
    return ctx, tuple(u for u in units if u.channel == "ADR")


class Capacity(RequestBudget):
    """Small additive provider-cost model isolates a known bin-packing counterexample.

    Requests themselves use the real canonical constructor/serializer. Actual
    tokenizer/window behavior is covered separately and by full-archive replay.
    """

    def __init__(self, units, weights, **settings):
        super().__init__(
            EvaluationSettings(max_input_tokens=10, provider_reserve=0, **settings), len
        )
        self.weights = {u.unit_id: w for u, w in zip(units, weights, strict=True)}
        self.calls = 0

    def count(self, request, phase):
        self.calls += 1
        return sum(self.weights[u.unit_id] for u in request.units)


def manifest(packets):
    return tuple(tuple(u.unit_id for u in p.units) for p in packets)


async def test_nonadjacent_backfill_preserves_four_independent_responsibilities():
    ctx, units = await context()
    budget = Capacity(units, (6, 6, 4, 4))
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted and len(packets) == 2
    assert manifest(packets) == (
        (units[0].unit_id, units[3].unit_id),
        (units[1].unit_id, units[2].unit_id),
    )
    assert {u.unit_id for p in packets for u in p.units} == {u.unit_id for u in units}
    assert all(budget.fits(p, "ASSESS") for p in packets)
    # Each unit remains an independent contract with its original support.
    singles = {
        u.unit_id: make_request(ctx, (u,), EvidenceBundle(), ()).units[0] for u in units
    }
    assert all(u == singles[u.unit_id] for p in packets for u in p.units)


async def test_input_permutations_produce_identical_packets():
    ctx, units = await context()
    baseline = None
    for order in itertools.permutations(units):
        packets, omitted = Capacity(units, (6, 6, 4, 4)).pack(
            ctx, order, EvidenceBundle(), ()
        )
        assert not omitted
        projection = [provider_request_json(p, "ASSESS") for p in packets]
        baseline = projection if baseline is None else baseline
        assert projection == baseline


async def test_backfill_does_not_cross_changed_subject_boundaries():
    ctx, units = await context()
    units = (
        units[0].model_copy(update={"delta_ids": ("!independent-subject",)}),
        *units[1:],
    )
    packets, omitted = Capacity(units, (6, 6, 4, 4)).pack(
        ctx, units, EvidenceBundle(), ()
    )
    assert not omitted and len(packets) == 3
    assert not any(
        units[0].unit_id in {u.unit_id for u in p.units}
        and units[3].unit_id in {u.unit_id for u in p.units}
        for p in packets
    )


@pytest.mark.parametrize(
    "weights,expected_omitted", [((10, 6, 4, 4), 0), ((11, 6, 4, 4), 1)]
)
async def test_near_limit_and_oversized_units_are_not_truncated(
    weights, expected_omitted
):
    ctx, units = await context()
    budget = Capacity(units, weights)
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert len(omitted) == expected_omitted
    assert {u.unit_id for p in packets for u in p.units} | {
        u.unit_id for u in omitted
    } == {u.unit_id for u in units}
    assert all(budget.count(p, "ASSESS") <= 10 for p in packets)
    if not expected_omitted:
        assert any(
            len(p.units) == 1 and p.units[0].unit_id == units[0].unit_id
            for p in packets
        )


async def test_shared_and_differing_source_membership_is_lossless():
    ctx, units = await context()
    assert units[0].evidence_ids != units[3].evidence_ids
    packets, omitted = Capacity(units, (6, 6, 4, 4)).pack(
        ctx, units, EvidenceBundle(), ()
    )
    assert not omitted
    for p in packets:
        import json

        wire = json.loads(provider_request_json(p, "ASSESS"))
        expanded = expand_source_texts(wire)
        assert sorted(expanded["evidence"], key=lambda e: e["evidence_id"]) == [
            e.model_dump(mode="json") for e in p.evidence
        ]
        assert len({e.evidence_id for e in p.evidence}) == len(p.evidence)
        ev = {e.evidence_id: e for e in p.evidence}
        for u in p.units:
            expected = make_request(
                ctx,
                (next(v for v in units if v.unit_id == u.unit_id),),
                EvidenceBundle(),
                (),
            ).units[0]
            assert u.evidence_ids == expected.evidence_ids
            for eid in u.evidence_ids:
                if ev[eid].excerpt:
                    q = QuoteRef(evidence_id=eid, quote=ev[eid].excerpt[:100])
                    citation = resolve_quote(q, ev)
                    assert citation.evidence_id == eid and citation.quote == q.quote


async def test_real_serializer_and_output_window_remain_authoritative():
    ctx, units = await context()
    counter = lambda text: (len(text.encode()) + 3) // 4
    original = RequestBudget(EvaluationSettings(max_input_tokens=20000), counter)
    single_cost = max(
        original.count(make_request(ctx, (u,), EvidenceBundle(), ()), "ASSESS")
        for u in units
    )
    # Actual serialized requests, reserve and schema/prompt are counted.
    budget = RequestBudget(
        EvaluationSettings(
            max_input_tokens=20000,
            model_window=single_cost + 5000,
            max_output_tokens=5000,
        ),
        counter,
    )
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted
    assert all(
        budget.count(p, "ASSESS") + 5000 <= budget.settings.model_window
        for p in packets
    )
    assert budget.input_limit == single_cost


@pytest.mark.parametrize("max_units", [1, 12, 16])
async def test_batch_limit_never_constructs_an_illegal_seventeenth_unit(max_units):
    ctx, initial = await context()
    units = tuple(
        initial[0].model_copy(update={"unit_id": f"unit-{i:04}"}) for i in range(33)
    )
    packets, omitted = Capacity(
        units, (0,) * len(units), max_batch_units=max_units
    ).pack(ctx, units, EvidenceBundle(), ())
    assert not omitted
    assert sum(len(p.units) for p in packets) == len(units)
    assert all(len(p.units) <= max_units for p in packets)


async def test_duplicate_identity_is_rejected_before_manifest_ambiguity():
    ctx, units = await context()
    with pytest.raises(EvaluationInputError, match="Duplicate"):
        Capacity(units, (6, 6, 4, 4)).pack(
            ctx, (*units, units[0]), EvidenceBundle(), ()
        )


async def test_rkg_only_contract_and_source_roles_are_preserved():
    from ai_services.tests.test_decision_guard_context import changes

    graph, source = fixtures(adr=False)
    ctx = await ArchitectureContextBuilder(
        graph, source, Readiness(), lambda _: asyncio.Lock()
    ).build(changes())
    units, _ = build_units(ctx, 20)
    packets, omitted = RequestBudget(
        EvaluationSettings(max_input_tokens=20000), lambda t: len(t) // 4
    ).pack(ctx, units, EvidenceBundle(), ())
    assert not omitted and len(packets) == 1
    assert all(u.channel == "RKG" for u in packets[0].units)
    assert {e.origin for e in packets[0].evidence if e.kind == "CODE"} >= {
        "MERGE_BASE",
        "TARGET_BASE",
        "PR_HEAD",
    }


async def test_large_pr_has_linear_bounded_exact_cost_checks():
    ctx, initial = await context()
    units = tuple(
        initial[0].model_copy(update={"unit_id": f"unit-{i:04}"}) for i in range(512)
    )
    budget = Capacity(units, (6,) * len(units))
    packets, omitted = budget.pack(ctx, units, EvidenceBundle(), ())
    assert not omitted and len(packets) == 512
    assert budget.calls <= 14 * len(units)
