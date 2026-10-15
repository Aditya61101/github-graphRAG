"""Unified contracts through production orchestration, using independent fixtures."""

import json

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    EvaluationResult,
    EvaluationSettings,
    MissingFact,
)
from ai_services.decision_guard.evaluation.wire import WireAssessment, compact
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    evaluate,
    fixture,
)


class MutateModel(ScriptedModel):
    def __init__(self, *, assess_edit=None, review_edit=None, **kwargs):
        super().__init__({("ADR", "service.py"): "conflict"}, **kwargs)
        self.assess_edit, self.review_edit = assess_edit, review_edit

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)
        data = json.loads(reply.output_json)
        if self.assess_edit:
            self.assess_edit(data, request)
        return reply.model_copy(update={"output_json": json.dumps(data)})

    async def challenge(self, request, **kwargs):
        reply = await super().challenge(request, **kwargs)
        data = json.loads(reply.output_json)
        if self.review_edit:
            self.review_edit(data, request)
        return reply.model_copy(update={"output_json": json.dumps(data)})


def issue(data):
    return next(u["issues"][0] for u in data["units"] if u["issues"])


async def test_rule_once_no_clearance_placement_still_reaches_independent_review():
    ctx, source = await fixture()
    model = MutateModel()
    result = await evaluate(ctx, source, model)
    assert result.outcome == "CONFLICTS_FOUND"
    assert len(model.challenge_requests) == 1
    finding = result.findings[0]
    assert finding.review and finding.review.verdict == "SUPPORTED"
    assert finding.candidate.rule.citations
    assert "claims" not in type(finding.candidate).model_fields
    assert not any("CLEARANCE" in p.obligation for p in result.proofs)
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "fault",
    [
        "fabricated",
        "wrong_revision",
        "wrong_rule",
        "wrong_observation",
        "wrong_core_revision",
    ],
)
async def test_rejected_finding_isolated_from_valid_sibling(fault):
    ctx, source = await fixture()

    def edit(data, request):
        unit = next(u for u in data["units"] if u["issues"])
        bad = json.loads(json.dumps(unit["issues"][0]))
        bad["claim"] = "Separate invalid architectural allegation"
        if fault == "fabricated":
            bad["core"]["citations"] = [{"evidence_id": "invented", "quote": "fiction"}]
        elif fault == "wrong_revision":
            head = next(r for r in bad["revisions"] if r["revision"] == "PR_HEAD")
            head["citations"] = next(
                r["citations"]
                for r in bad["revisions"]
                if r["revision"] == "MERGE_BASE"
            )
        elif fault == "wrong_core_revision":
            bad["core"]["citations"] = next(
                r["citations"]
                for r in bad["revisions"]
                if r["revision"] == "MERGE_BASE"
            )
            bad["core"]["observation_ids"] = [
                o.observation_id
                for o in request.observations
                if o.revision == "PR_HEAD" and o.path == "service.py"
            ]
        elif fault == "wrong_rule":
            bad["rule"]["citations"] = bad["core"]["citations"]
        else:
            bad["core"]["observation_ids"] = ["invented-observation"]
        unit["issues"].append(bad)

    model = MutateModel(assess_edit=edit)
    result = await evaluate(ctx, source, model)
    assert (
        len(result.findings) == 1
        and result.findings[0].disposition == "SUPPORTED_CONFLICT"
    )
    assert len(result.rejected_findings) == 1 and result.rejected_findings[0].raw_json
    assert result.coverage.uncertain == 1 and result.coverage.scope == "PARTIAL"
    assert len(model.challenge_requests[0].candidates) == 1
    assert result.outcome == "CONFLICTS_FOUND"


@pytest.mark.parametrize(
    "fault", ["INSUFFICIENT", "fabricated", "missing", "core_fact"]
)
async def test_review_absence_failure_or_material_uncertainty_never_confirms(fault):
    ctx, source = await fixture()

    def edit(data, request):
        review = data["decisions"][0]
        if fault == "INSUFFICIENT":
            review["verdict"] = "INSUFFICIENT"
        elif fault == "fabricated":
            review["citations"] = [{"evidence_id": "fiction", "quote": "no rule"}]
        elif fault == "missing":
            data["decisions"] = []
        else:
            review["missing_facts"] = [
                MissingFact(
                    fact_id="reachable",
                    scope="CORE",
                    fact="Whether witness reaches the boundary",
                    relevance="Required for this allegation",
                    unrequestable_reason="No authorized source anchor",
                ).model_dump(mode="json")
            ]

    result = await evaluate(
        ctx,
        source,
        MutateModel(review_edit=edit),
        settings=EvaluationSettings(retries=0),
    )
    assert result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.findings[0].review_error


async def test_unknown_quote_in_one_issue_is_audited_without_dropping_sibling():
    ctx, source = await fixture()

    class BadQuote(MutateModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            wire = compact(
                AssessmentBatchOutput.model_validate_json(reply.output_json), request
            ).model_dump(mode="json")
            u = next(u for u in wire["units"] if u["issues"])
            bad = json.loads(json.dumps(u["issues"][0]))
            bad["claim"] = "Unknown quote ID in independent finding"
            bad["core"]["citations"] = ["H9999"]
            u["issues"].append(bad)
            expanded = WireAssessment.model_validate_json(json.dumps(wire)).expanded(
                request
            )
            assert expanded.units[0].rejections
            return reply.model_copy(update={"output_json": expanded.model_dump_json()})

    result = await evaluate(ctx, source, BadQuote())
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    assert result.rejected_findings[0].reason == "UNKNOWN_QUOTE_ID"
    assert result.coverage.uncertain == 1


async def test_final_artifact_cannot_fake_confirmation_or_hide_rejected_coverage():
    ctx, source = await fixture()
    result = await evaluate(ctx, source, MutateModel(challenge="INSUFFICIENT"))
    data = json.loads(result.model_dump_json())
    data["findings"][0]["disposition"] = "SUPPORTED_CONFLICT"
    data["outcome"] = "CONFLICTS_FOUND"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate_json(json.dumps(data))


async def test_no_challenge_budget_remains_unresolved_without_extra_calls():
    ctx, source = await fixture()
    model = MutateModel()
    result = await evaluate(
        ctx, source, model, settings=EvaluationSettings(max_attempts=1, retries=0)
    )
    assert len(result.calls) == 1 and not model.challenge_requests
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.outcome == "REVIEW_REQUIRED"


async def test_same_original_source_available_but_semantically_insufficient_is_not_clean():
    ctx, source = await fixture()
    model = MutateModel(challenge="INSUFFICIENT")
    result = await evaluate(ctx, source, model)
    assert result.findings[0].review.verdict == "INSUFFICIENT"
    assert result.coverage.uncertain == 1
    assert result.outcome == "REVIEW_REQUIRED"


async def test_one_invalid_review_does_not_suppress_other_supported_finding():
    ctx, source = await fixture()

    def duplicate(data, request):
        u = next(u for u in data["units"] if u["issues"])
        second = json.loads(json.dumps(u["issues"][0]))
        second["claim"] = "Independent second local allegation"
        u["issues"].append(second)

    def corrupt(data, request):
        data["decisions"][0]["citations"] = [
            {"evidence_id": "fabricated", "quote": "false"}
        ]

    result = await evaluate(
        ctx, source, MutateModel(assess_edit=duplicate, review_edit=corrupt)
    )
    assert {f.disposition for f in result.findings} == {
        "SUPPORTED_CONFLICT",
        "NEEDS_REVIEW",
    }
    assert result.coverage.uncertain == 1 and result.coverage.scope == "PARTIAL"


@pytest.mark.parametrize("attribution", ["PRE_EXISTING", "UNCHANGED"])
async def test_material_uncertainty_in_nonnew_issue_cannot_produce_clean_pr(
    attribution,
):
    ctx, source = await fixture()

    def edit(data, request):
        i = issue(data)
        i["missing_facts"] = [
            MissingFact(
                fact_id="material",
                scope="CORE",
                fact="Whether relevant safeguard exists",
                relevance="Necessary to resolve this allegation",
                unrequestable_reason="No authorized source provided",
            ).model_dump(mode="json")
        ]

    result = await evaluate(
        ctx, source, MutateModel(assess_edit=edit, attribution=attribution)
    )
    assert result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.coverage.uncertain == 1
