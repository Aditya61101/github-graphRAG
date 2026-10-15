"""Source-revalidated assessment reuse and pre-dispatch denial accounting."""

import json

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation import DecisionIntegrityEvaluator
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    CallRecord,
    DispatchDenied,
    EvaluationInputError,
    EvaluationResult,
    EvaluationSettings,
    PreservedAssessment,
)
from ai_services.decision_guard.models import digest
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    evaluate,
    fixture,
    tokens,
)


@pytest.mark.parametrize(
    "reason,state",
    [
        ("AUTHORIZATION_DENIED", "UNREVIEWED_AUTHORIZATION"),
        ("BUDGET_DENIED", "UNREVIEWED_BUDGET"),
    ],
)
async def test_predispatch_denial_is_not_provider_failure_or_unreported_usage(
    reason, state
):
    ctx, source = await fixture()

    class Denied(ScriptedModel):
        attempts = 0

        async def challenge(self, request, **kwargs):
            self.attempts += 1
            raise DispatchDenied(reason)

    model = Denied({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, settings=EvaluationSettings(retries=1))
    call = result.calls[-1]
    assert call.status == reason and call.denied_reservation_tokens > 0
    assert (
        call.input_tokens == call.output_tokens == call.unreported_reserved_tokens == 0
    )
    assert result.reserved_unreported_tokens == 0 and model.attempts == 1
    assert result.findings[0].challenge_state == state
    assert result.findings[0].candidate and result.outcome == "REVIEW_REQUIRED"
    assert reason in result.units[0].reason_codes
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "error,status",
    [(RuntimeError("lost reply"), "MODEL_ERROR"), (TimeoutError(), "MODEL_TIMEOUT")],
)
async def test_uncertain_dispatch_retains_reservation(error, status):
    ctx, source = await fixture()

    class Failed(ScriptedModel):
        async def challenge(self, request, **kwargs):
            raise error

    result = await evaluate(
        ctx,
        source,
        Failed({("ADR", "service.py"): "conflict"}),
        settings=EvaluationSettings(retries=0),
    )
    call = result.calls[-1]
    assert call.status == status and call.unreported_reserved_tokens > 0
    assert call.denied_reservation_tokens == 0 and result.reserved_unreported_tokens > 0
    assert result.outcome == "REVIEW_REQUIRED"


def test_denied_call_cannot_claim_inflight_or_provider_usage():
    with pytest.raises(ValidationError, match="Pre-dispatch denial"):
        CallRecord(
            batch_id="b",
            phase="CHALLENGE",
            status="AUTHORIZATION_DENIED",
            input_tokens=0,
            output_tokens=0,
            unreported_reserved_tokens=10,
            denied_reservation_tokens=10,
            elapsed_ms=0,
        )


async def archived():
    ctx, source = await fixture()
    original = ScriptedModel({("ADR", "service.py"): "conflict"})
    baseline = await evaluate(ctx, source, original)
    request = original.requests[0]
    reply = await original.assess(request)
    reply = reply.model_copy(update={"request_id": "preserved-provider-response"})
    preserved = PreservedAssessment(
        request=request,
        reply=reply,
        provider_response_sha256=digest("original provider response"),
    )
    return ctx, source, preserved, baseline


class ChallengeOnly(ScriptedModel):
    async def assess(self, *args, **kwargs):
        raise AssertionError("Assessor must never run")


async def test_durable_ledger_does_not_charge_a_known_predispatch_denial(tmp_path):
    from ai_services.runs.run_stage_4_2_live_validation import AggregateModel

    _, _, preserved, _ = await archived()

    class Denied(ScriptedModel):
        async def assess(self, request, **kwargs):
            raise DispatchDenied("AUTHORIZATION_DENIED")

    ledger = AggregateModel(Denied(), tmp_path, lambda text: 1)
    with pytest.raises(DispatchDenied):
        await ledger.assess(preserved.request, max_output_tokens=5000, timeout=1)
    entry = json.loads((tmp_path / "aggregate_usage.json").read_text())["calls"][0]
    assert entry["status"] == "AUTHORIZATION_DENIED" and entry["charged_tokens"] == 0
    assert entry["denied_reservation_tokens"] == entry["reserved_tokens"] == 5513


async def resume(ctx, source, preserved, model):
    return await DecisionIntegrityEvaluator(
        model, source=source, authority=TrustedAuthority(), token_counter=tokens
    ).evaluate(ctx, preserved_assessment=preserved)


async def test_reuse_runs_real_aggregation_without_assessment_call_or_double_charging():
    ctx, source, preserved, baseline = await archived()
    model = ChallengeOnly()
    result = await resume(ctx, source, preserved, model)
    assert result.outcome == "CONFLICTS_FOUND" and len(model.challenge_requests) == 1
    assert [c.phase for c in result.calls] == ["CHALLENGE"]
    assert (result.input_tokens, result.output_tokens) == (1100, 400)
    assert [(f.finding_id, f.candidate) for f in result.findings] == [
        (f.finding_id, f.candidate) for f in baseline.findings
    ]
    record = json.loads(result.validation_records_json)["preserved_assessment"]
    assert record["provider_request_id"] == preserved.reply.request_id
    assert record["usage_scope"] == "HISTORICAL_NOT_CHARGED_AGAIN"
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("fault", ["request", "manifest", "citation"])
async def test_reuse_revalidates_identity_manifest_and_original_evidence(fault):
    ctx, source, preserved, _ = await archived()
    model = ChallengeOnly()
    if fault == "request":
        preserved = preserved.model_copy(
            update={
                "request": preserved.request.model_copy(
                    update={"context_hash": "f" * 64}
                )
            }
        )
    else:
        data = json.loads(preserved.reply.output_json)
        if fault == "manifest":
            data["batch_id"] = "wrong-batch"
        else:
            p = next(p for p in data["units"] if p["issues"])
            p["issues"][0]["core"]["citations"] = [
                {"evidence_id": "invented", "quote": "fabricated"}
            ]
        output = AssessmentBatchOutput.model_validate_json(json.dumps(data))
        preserved = preserved.model_copy(
            update={
                "reply": preserved.reply.model_copy(
                    update={"output_json": output.model_dump_json()}
                )
            }
        )
    if fault == "citation":
        result = await resume(ctx, source, preserved, model)
        assert result.outcome == "REVIEW_REQUIRED" and result.rejected_findings
    else:
        with pytest.raises(EvaluationInputError):
            await resume(ctx, source, preserved, model)
    assert not model.challenge_requests
