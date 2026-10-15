"""Offline historical replay, focused-packet sizing and source-preservation audit.

Uses preserved responses and a scripted source-execution oracle, never a provider.
Run from backend: python -m ai_services.runs.validate_stage_4_2_challenger
"""

import asyncio
import hashlib
import json
import sys
import time
import types
from pathlib import Path

import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.challenge import expand_challenge_sources
from ai_services.decision_guard.evaluation.engine import (
    Pending,
    RequestBudget,
    challenge_request,
    plan_challenges,
)
from ai_services.decision_guard.evaluation.evidence import (
    resolve_quote,
    validate_context,
)
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
    PROMPT_VERSION,
    wire_text,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    ChallengeBatchOutput,
    ChallengeRequest,
    EvaluationResult,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_issue,
    validate_review,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
    provider_request_json,
)
from ai_services.decision_guard.models import ArchitectureContext, digest, stable_id
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.validate_stage_4_2_unified import CaseAScript
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

TAG = "A_challenger_hardened_offline"


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mean_ms(fn):
    start = time.perf_counter()
    for _ in range(10):
        fn()
    return (time.perf_counter() - start) * 100


async def run():
    if list(ROOT.glob(TAG + "_*.json")):
        raise FileExistsError("Preserve existing offline audit artifacts")
    baseline = read("A_challenger_v1_baseline.json")
    protected = baseline["protected_sha256"]
    assert all(file_hash(ROOT / p) == h for p, h in protected.items())
    authority_record = read("A_revision_aware_live_v1_authorization.json")
    upstream = {
        p: h
        for p, h in authority_record["production_hashes"].items()
        if "/evaluation/" not in p.replace("\\", "/")
    }
    assert all(file_hash(Path(p)) == h for p, h in upstream.items())
    ledger = read("aggregate_usage.json")
    assert (
        len(ledger["calls"]) == 7
        and sum(c["charged_tokens"] for c in ledger["calls"]) == 134815
    )
    assert ledger["max_tokens"] == 142658
    assert (
        file_hash(ROOT / "aggregate_usage.json")
        == "2678587996b5936e351fe99f524b90cb1525e41c0cf4b5aa43e1280110f03a40"
    )

    # Execute only the trusted, preserved pure serializer; no client construction.
    old = types.ModuleType(
        "ai_services.decision_guard.evaluation._offline_challenger_baseline"
    )
    old.__package__ = "ai_services.decision_guard.evaluation"
    sys.modules[old.__name__] = old
    source = next(v for k, v in baseline["source"].items() if k.endswith("wire.py"))
    exec(compile(source, "preserved-pre-hardening-wire.py", "exec"), old.__dict__)  # noqa: S102
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda s: len(enc.encode(s, disallowed_special=())),
    )
    settings = EvaluationSettings.model_validate(
        read("manifest.json")["settings"]
    ).model_copy(update={"max_attempts": 2, "retries": 0, "concurrency": 1})
    budget = RequestBudget(settings, counter)

    def old_text(req, phase):
        return (
            baseline["assessment_prompt" if phase == "ASSESS" else "challenge_prompt"]
            + old.provider_request_json(req, phase)
            + json.dumps(
                baseline[
                    "assessment_schema" if phase == "ASSESS" else "challenge_schema"
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    req06 = AssessmentRequest.model_validate_json(
        (ROOT / "call_06.request.json").read_text()
    )
    req07 = ChallengeRequest.model_validate_json(
        (ROOT / "call_07.request.json").read_text()
    )
    raw06 = read("call_06.provider.json")["choices"][0]["message"]["content"]
    raw07 = read("call_07.provider.json")["choices"][0]["message"]["content"]
    assessment = WireAssessment.model_validate_json(raw06).expanded(req06)
    assert assessment == AssessmentBatchOutput.model_validate_json(
        read("call_06.reply.json")["output_json"]
    )
    historical_review = old.WireChallenge.model_validate_json(raw07).expanded(req07)
    assert historical_review == ChallengeBatchOutput.model_validate_json(
        read("call_07.reply.json")["output_json"]
    )
    try:
        WireChallenge.model_validate_json(raw07)
    except ValidationError:
        old_contract_rejected = True
    else:
        raise AssertionError(
            "Old quote responses must not be silently repaired or reinterpreted"
        )
    old_quotes = json.loads(raw07)["quotes"]
    old_sources = old.source_references(req07)
    failures = []
    from ai_services.decision_guard.evaluation.models import QuoteRef

    for quote in old_quotes:
        e = old_sources[quote["source_ref"]]
        try:
            resolve_quote(
                QuoteRef(evidence_id=e.evidence_id, quote=quote["quote"]),
                {e.evidence_id: e},
            )
        except ValueError as error:
            failures.append({"quote_id": quote["quote_id"], "reason": str(error)})
    assert (
        len(old_quotes) == 9
        and len(failures) == 7
        and all(f["reason"] == "QUOTE_NOT_FOUND" for f in failures)
    )
    saved_result = EvaluationResult.model_validate_json(
        (ROOT / "A_revision_aware_live_v1_result.json").read_text()
    )
    assert saved_result.outcome == "REVIEW_REQUIRED"
    old_decision = historical_review.decisions[0]
    old_unit = next(
        u for u in req07.assessment.units if u.unit_id == req07.candidates[0].unit_id
    )
    try:
        validate_review(
            old_decision,
            req07.assessment,
            old_unit,
            req07.candidates[0].proposal_id,
            req07.candidates[0].issue,
        )
    except ValueError as error:
        assert str(error) == "QUOTE_NOT_FOUND"
    else:
        raise AssertionError("Unmodified failed challenge must remain rejected")

    ctx = validate_context(
        ArchitectureContext.model_validate_json(
            (ROOT / "A_unified_v7_certified_offline_context.json").read_text()
        )
    )
    # Reproduce the production supplement, rather than treating existing context
    # evidence as additional supplements (which would incorrectly broaden units).
    manifest = read("manifest.json")
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    reader, _ = source_for(ctx.revision.repository_id, Path(case["source_root"]))
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    from ai_services.decision_guard.evaluation.engine import prepare_evaluation_evidence

    receipts, _, _, bundle, _ = await prepare_evaluation_evidence(
        ctx, settings, reader, authority
    )
    items = []
    for proposal in assessment.units:
        unit = next(u for u in req06.units if u.unit_id == proposal.unit_id)
        for index, issue in enumerate(proposal.issues):
            key = stable_id(unit.unit_id, str(index), issue.claim)
            proofs, citations, attribution, eligible = validate_issue(
                proposal, issue, req06, unit, key
            )
            assert eligible
            items.append(
                Pending(
                    unit, proposal, issue, key, proofs, citations, attribution, eligible
                )
            )
    assert len(items) == 2
    original_adr = next(p for p in items if p.unit.channel == "ADR")
    reproduced = challenge_request(ctx, (original_adr,), bundle, receipts)
    assert reproduced == req07
    combined = challenge_request(ctx, items, bundle, req06.authority)
    packets, omitted = plan_challenges(ctx, items, bundle, req06.authority, budget)
    assert not omitted and len(packets) == 1 and len(packets[0].candidates) == 2
    # Historical planner flushed its current group when the combined request overflowed.
    old_packets, current = [], []
    for item in items:
        probe = challenge_request(ctx, (*current, item), bundle, req06.authority)
        if (
            current
            and counter(old_text(probe, "CHALLENGE")) + settings.provider_reserve
            > budget.input_limit
        ):
            old_packets.append(challenge_request(ctx, current, bundle, req06.authority))
            current = []
        current.append(item)
    if current:
        old_packets.append(challenge_request(ctx, current, bundle, req06.authority))
    assert len(old_packets) == 2

    def metric(req, phase):
        return {
            "old_input_tokens": counter(old_text(req, phase))
            + settings.provider_reserve,
            "new_input_tokens": budget.count(req, phase),
            "maximum_output_tokens": settings.max_output_tokens,
            "new_fits": budget.fits(req, phase),
            "old_serialization_ms": mean_ms(lambda: old_text(req, phase)),
            "new_serialization_ms": mean_ms(lambda: wire_text(req, phase)),
            "canonical_request_sha256": digest(req.model_dump_json()),
            "provider_payload_sha256": digest(provider_request_json(req, phase)),
        }

    metrics = {
        "assessment": metric(req06, "ASSESS"),
        "historical_single_challenge": metric(req07, "CHALLENGE"),
        "both_historical_candidates": metric(combined, "CHALLENGE"),
    }
    assert (
        metrics["assessment"]["old_input_tokens"]
        == metrics["assessment"]["new_input_tokens"]
        == 19173
    )
    assert metrics["historical_single_challenge"]["old_input_tokens"] == 19548
    payload = json.loads(provider_request_json(combined, "CHALLENGE"))
    restored = expand_challenge_sources(payload)
    originals = {e.evidence_id: e for e in combined.assessment.evidence}
    assert all(originals[e.evidence_id] == e for e in restored)
    assert {e.evidence_id for e in old.source_references(combined).values()} <= {
        e.evidence_id for e in restored
    }

    # The preserved ClaimIQ source-execution oracle is a test, not model reasoning.
    class SpanOracle(CaseAScript):
        async def challenge(self, req, **kwargs):
            reply = await super().challenge(req, **kwargs)
            self.source_review = ChallengeBatchOutput.model_validate_json(
                reply.output_json
            )
            self.span_review = compact(self.source_review, req)
            # Exact expansion through the production wire parser, without a provider.
            expanded = self.span_review.expanded(req)
            return reply.model_copy(
                update={
                    "output_json": expanded.model_dump_json(),
                    "wire_output_json": self.span_review.model_dump_json(),
                }
            )

    model = SpanOracle()
    result = await DecisionIntegrityEvaluator(
        model,
        source=reader,
        authority=authority,
        settings=settings,
        token_counter=counter,
    ).evaluate(ctx)
    assert (
        result.outcome == "CONFLICTS_FOUND"
        and result.findings[0].disposition == "SUPPORTED_CONFLICT"
    )
    assert len(model.requests) == len(model.challenge_requests) == 1
    out_req = model.challenge_requests[0]
    old_output = old.compact(model.source_review, out_req).model_dump_json()
    new_output = model.span_review.model_dump_json()
    for schema in (WireAssessment, WireChallenge):
        to_strict_json_schema(schema)
    assert ASSESS_PROMPT == baseline["assessment_prompt"]
    assert WireAssessment.model_json_schema() == baseline["assessment_schema"]
    assert all(file_hash(ROOT / p) == h for p, h in protected.items())
    assert all(file_hash(Path(p)) == h for p, h in upstream.items())
    summary = {
        "offline_only": True,
        "billable_calls": 0,
        "prompt_version": PROMPT_VERSION,
        "prompt_hashes": {
            "assessment": digest(ASSESS_PROMPT),
            "challenge": digest(CHALLENGE_PROMPT),
        },
        "schema_hashes": {
            "assessment": digest(
                json.dumps(WireAssessment.model_json_schema(), sort_keys=True)
            ),
            "challenge": digest(
                json.dumps(WireChallenge.model_json_schema(), sort_keys=True)
            ),
        },
        "historical_replay": {
            "call_06_exact_expansion": True,
            "call_07_exact_legacy_expansion": True,
            "call_07_literal_failures": failures,
            "old_challenge_wire_rejected_by_new_contract": old_contract_rejected,
            "original_result": saved_result.outcome,
            "witness_correspondence": old_decision.witness_correspondence,
            "missing_facts": [
                f.model_dump(mode="json") for f in old_decision.missing_facts
            ],
        },
        "input_metrics": metrics,
        "packing": {
            "eligible_candidates": len(items),
            "old_packets": len(old_packets),
            "old_packet_inputs": [
                counter(old_text(p, "CHALLENGE")) + settings.provider_reserve
                for p in old_packets
            ],
            "new_packets": len(packets),
            "new_packet_inputs": [budget.count(p, "CHALLENGE") for p in packets],
            "new_candidate_ids": [
                [c.proposal_id for c in p.candidates] for p in packets
            ],
            "omitted": omitted,
            "expected_total_calls_before": 3,
            "expected_total_calls_after": 2,
            "candidates_reviewable_with_two_call_cap_before": 1,
            "candidates_reviewable_with_two_call_cap_after": 2,
        },
        "projection": {
            **payload["projection"],
            "displayed_sources": len(restored),
            "shared_texts": len(payload["source_texts"]),
            "restored_originals_exact": True,
            "old_payload_components": {
                k: counter(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
                for k, v in json.loads(
                    old.provider_request_json(req07, "CHALLENGE")
                ).items()
            },
        },
        "output_comparison_scripted_same_decision": {
            "old_tokens": counter(old_output),
            "new_tokens": counter(new_output),
            "old_quote_characters": sum(
                len(q["quote"]) for q in json.loads(old_output)["quotes"]
            ),
            "new_quote_characters": 0,
            "new_span_count": len(model.span_review.spans),
            "maximum_output_unchanged": settings.max_output_tokens,
        },
        "scripted_claimiq": {
            "outcome": result.outcome,
            "finding_dispositions": [f.disposition for f in result.findings],
            "coverage": result.coverage.model_dump(mode="json"),
            "calls": len(result.calls),
            "not_a_model_reasoning_validation": True,
        },
        "ledger": {
            "calls": 7,
            "charged_tokens": 134815,
            "ceiling": 142658,
            "remaining": 7843,
            "sha256": file_hash(ROOT / "aggregate_usage.json"),
            "unchanged": True,
        },
        "preservation": {
            "historical_json_files": len(protected),
            "unchanged": True,
            "upstream_source_files": len(upstream),
            "upstream_unchanged": True,
        },
        "provider_latency_or_savings_measured": False,
    }
    artifacts = {
        "summary": summary,
        "combined_request": combined.model_dump(mode="json"),
        "combined_projection": payload,
        "scripted_result": result.model_dump(mode="json"),
        "scripted_span_response": model.span_review.model_dump(mode="json"),
    }
    for name, data in artifacts.items():
        with (ROOT / f"{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
