"""Witness correspondence and conservative final scope through production flow."""

import json

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation.models import (
    ChallengeBatchOutput,
    EvaluationResult,
    EvaluationSettings,
    MissingFact,
)
from ai_services.decision_guard.evaluation.wire import WireChallenge, compact
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    changed,
    evaluate,
    fixture,
)
from ai_services.tests.test_unified_evaluation import MutateModel


async def test_unchanged_witness_is_preserved_and_independently_supported():
    ctx, source = await fixture()
    model = MutateModel()
    result = await evaluate(ctx, source, model)
    finding = result.findings[0]
    assert finding.disposition == "SUPPORTED_CONFLICT"
    assert finding.review.witness_correspondence == "UNCHANGED"
    assert finding.candidate.core.witness == finding.core.witness
    assert finding.review.tested_witness
    assert finding.review.citations and finding.review.revisions
    wire = compact(
        ChallengeBatchOutput(batch_id="audit", decisions=(finding.review,)),
        model.challenge_requests[0],
    )
    resolved = (
        WireChallenge.model_validate_json(wire.model_dump_json())
        .expanded(model.challenge_requests[0])
        .decisions[0]
    )
    assert resolved.tested_witness == finding.review.tested_witness
    assert [(c.evidence_id, c.quote) for c in resolved.citations] == [
        (c.evidence_id, c.quote) for c in finding.review.citations
    ]
    assert all(c.start_char is not None for c in resolved.citations)
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("correspondence", ["MATERIAL_CHANGE", "UNCERTAIN", None])
async def test_supported_vote_cannot_override_changed_or_unknown_witness(
    correspondence,
):
    ctx, source = await fixture()

    def edit(data, request):
        decision = data["decisions"][0]
        decision["tested_witness"] = (
            "An alternative state with different qualifying premises"
        )
        if correspondence:
            decision["witness_correspondence"] = correspondence
        else:
            decision.pop("witness_correspondence")

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    finding = result.findings[0]
    assert finding.review.verdict == "SUPPORTED"  # Preserve the original vote.
    assert finding.disposition == "NEEDS_REVIEW"  # Effective result is insufficient.
    assert finding.review_error == "WITNESS_CORRESPONDENCE_" + (
        correspondence or "UNCERTAIN"
    )
    assert (
        finding.review_error
        in next(u for u in result.units if u.unit_id == finding.unit_id).reason_codes
    )
    assert finding.candidate.core.witness != finding.review.tested_witness
    assert result.outcome == "REVIEW_REQUIRED" and result.coverage.uncertain == 1
    assert not any(
        p.supported for p in result.proofs if p.method == "SEMANTIC_CHALLENGE"
    )


async def test_consistency_assertion_without_original_witness_source_is_insufficient():
    ctx, source = await fixture()

    def edit(data, request):
        decision = data["decisions"][0]
        decision["citations"] = [
            r
            for r in decision["citations"]
            if r["evidence_id"]
            not in {r.evidence_id for r in request.candidates[0].issue.core.citations}
        ]

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    assert result.findings[0].review.witness_correspondence == "UNCHANGED"
    assert result.findings[0].review_error == "WITNESS_ORIGINAL_SOURCE_NOT_REVIEWED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.outcome == "REVIEW_REQUIRED"


async def test_witness_paraphrase_and_different_literal_quote_need_no_string_equality():
    ctx, source = await fixture()

    def edit(data, request):
        decision = data["decisions"][0]
        decision["tested_witness"] = "The original boundary state, expressed concisely"
        for ref in decision["citations"]:
            ref["quote"] = ref["quote"][:20]

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    assert result.findings[0].core.witness != result.findings[0].review.tested_witness


async def test_same_witness_assertion_does_not_override_missing_material_evidence():
    ctx, source = await fixture()

    def edit(data, request):
        data["decisions"][0]["missing_facts"] = [
            MissingFact(
                fact_id="qualifier",
                scope="CORE",
                fact="Whether the subject qualifies",
                relevance="Required to establish the original witness",
                unrequestable_reason="No authorized source supports this premise",
            ).model_dump(mode="json")
        ]

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    assert result.findings[0].review.witness_correspondence == "UNCHANGED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.outcome == "REVIEW_REQUIRED"


@pytest.mark.parametrize("verdict", ["REFUTED", "INSUFFICIENT"])
async def test_witness_mismatch_is_audited_for_non_supporting_verdicts(verdict):
    ctx, source = await fixture()

    def edit(data, request):
        decision = data["decisions"][0]
        decision["verdict"] = verdict
        decision["witness_correspondence"] = "MATERIAL_CHANGE"

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.findings[0].review_error == "WITNESS_CORRESPONDENCE_MATERIAL_CHANGE"
    assert result.outcome == "REVIEW_REQUIRED"


async def test_independent_candidates_keep_supported_refuted_and_mismatched_reviews():
    ctx, source = await fixture()

    def duplicate(data, request):
        unit = next(u for u in data["units"] if u["issues"])
        original = unit["issues"][0]
        for number in (2, 3):
            other = json.loads(json.dumps(original))
            other["claim"] += f" {number}"
            unit["issues"].append(other)

    def edit(data, request):
        data["decisions"][1]["verdict"] = "REFUTED"
        data["decisions"][2]["witness_correspondence"] = "MATERIAL_CHANGE"

    result = await evaluate(
        ctx, source, MutateModel(assess_edit=duplicate, review_edit=edit)
    )
    assert {f.disposition for f in result.findings} == {
        "SUPPORTED_CONFLICT",
        "REFUTED",
        "NEEDS_REVIEW",
    }
    assert result.outcome == "CONFLICTS_FOUND"
    assert result.coverage.scope == "PARTIAL" and result.coverage.uncertain == 1


async def test_complete_clean_result_is_explicitly_scoped_without_challenge():
    ctx, source = await fixture()
    model = ScriptedModel()
    result = await evaluate(ctx, source, model)
    assert result.outcome == "NO_CONFLICTS_FOUND"
    assert result.coverage.scope == "COMPLETE_FOR_RETAINED_SCOPE"
    assert result.coverage.assessed == len(result.plan)
    assert result.execution == "COMPLETED" and not model.challenge_requests


@pytest.mark.parametrize("other", ["omitted", "uncertain", "error", "rejected"])
async def test_clean_siblings_never_hide_partial_evaluation(other):
    ctx, source = await fixture(
        files=[
            changed("service.py", "def a(): return 1\n", "def a(): return 2\n"),
            changed("other.py", "def b(): return 1\n", "def b(): return 2\n"),
        ]
    )

    class PartialModel(ScriptedModel):
        async def assess(self, request, **kwargs):
            if other == "error" and any(u.path == "other.py" for u in request.units):
                raise RuntimeError("offline provider failure")
            reply = await super().assess(request, **kwargs)
            data = json.loads(reply.output_json)
            if other == "rejected":
                for unit in data["units"]:
                    if (
                        next(
                            u for u in request.units if u.unit_id == unit["unit_id"]
                        ).path
                        == "other.py"
                    ):
                        unit["conclusion"] = "ISSUES"
                        unit["rejections"] = [
                            {
                                "subject_id": "invalid",
                                "reason": "UNKNOWN_QUOTE_ID",
                                "raw_json": "{}",
                            }
                        ]
            return reply.model_copy(update={"output_json": json.dumps(data)})

    settings = EvaluationSettings(
        max_batch_units=1,
        retries=0,
        concurrency=1,
        max_attempts=1 if other == "omitted" else 12,
    )
    model = PartialModel(
        {("ADR", "other.py"): "uncertain"} if other == "uncertain" else {}
    )
    result = await evaluate(ctx, source, model, settings=settings)
    assert result.coverage.assessed > 0
    assert result.outcome == "REVIEW_REQUIRED" and result.coverage.scope == "PARTIAL"
    assert (
        getattr(
            result.coverage,
            {"omitted": "omitted", "error": "errors"}.get(other, "uncertain"),
        )
        > 0
    )
    assert bool(result.rejected_findings) == (other == "rejected")
    assert not model.challenge_requests
    forged = json.loads(result.model_dump_json())
    forged["outcome"] = "NO_CONFLICTS_FOUND"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(forged)


@pytest.mark.parametrize(
    "fault", ["hide_finding", "hide_uncertainty", "fake_confirmation", "drop_finding"]
)
async def test_final_result_revalidates_witness_and_coverage_relationships(fault):
    ctx, source = await fixture()

    def edit(data, request):
        data["decisions"][0]["witness_correspondence"] = "UNCERTAIN"

    result = await evaluate(ctx, source, MutateModel(review_edit=edit))
    data = json.loads(result.model_dump_json())
    finding = data["findings"][0]
    unit = next(u for u in data["units"] if u["unit_id"] == finding["unit_id"])
    if fault == "hide_finding":
        unit["finding_ids"] = []
    elif fault == "drop_finding":
        data["findings"] = []
        unit["finding_ids"] = []
    elif fault == "hide_uncertainty":
        unit["status"] = "ASSESSED"
        data["coverage"]["assessed"] += 1
        data["coverage"]["uncertain"] -= 1
        data["coverage"]["scope"] = "COMPLETE_FOR_RETAINED_SCOPE"
        data["outcome"] = "NO_CONFLICTS_FOUND"
    else:
        finding["disposition"] = "SUPPORTED_CONFLICT"
        finding["review_error"] = None
        data["outcome"] = "CONFLICTS_FOUND"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(data)
