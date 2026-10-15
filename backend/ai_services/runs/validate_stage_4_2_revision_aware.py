"""Offline-only revision-contract replay and production serialization measurements."""

import ast
import asyncio
import hashlib
import json
import sys
import time
import types
import zipfile
from pathlib import Path

import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
    PROMPT_VERSION,
    wire_text,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import validate_issue
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
    provider_request_json,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.validate_stage_4_2_unified import CaseAScript
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

TAG = "A_revision_aware_v1_offline"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    with (ROOT / f"{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


async def run():
    if list(ROOT.glob(f"{TAG}_*.json")):
        raise FileExistsError("Preserve existing offline audit artifacts")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    baseline = json.loads(
        (ROOT / "A_controlled_v7_live_v2_authorization.json").read_text()
    )
    stage41 = {
        p: h
        for p, h in baseline["production_hashes"].items()
        if "/evaluation/" not in p.replace("\\", "/")
    }
    assert all(file_hash(Path(p)) == h for p, h in stage41.items())
    ledger = json.loads((ROOT / "aggregate_usage.json").read_text())
    assert (
        len(ledger["calls"]) == 5
        and sum(c["charged_tokens"] for c in ledger["calls"]) == 93485
    )
    assert ledger["max_tokens"] == 122658

    # Load the archived old wire implementation only for baseline measurement.
    # No provider/client is constructed by this module or its scripted oracle.
    with zipfile.ZipFile("../decisionguard_stage4_2_review.zip") as archive:
        source = archive.read(
            "backend/ai_services/decision_guard/evaluation/wire.py"
        ).decode()
        prompt_tree = ast.parse(
            archive.read(
                "backend/ai_services/decision_guard/evaluation/model_adapter.py"
            )
        )
    old = types.ModuleType(
        "ai_services.decision_guard.evaluation._offline_baseline_wire"
    )
    old.__package__ = "ai_services.decision_guard.evaluation"
    sys.modules[old.__name__] = old
    exec(  # noqa: S102 -- trusted archived pure serializer for offline comparison
        compile(source, "archived-original-wire.py", "exec"), old.__dict__
    )
    prompts = {
        n.targets[0].id: ast.literal_eval(n.value)
        for n in prompt_tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id in {"ASSESS_PROMPT", "CHALLENGE_PROMPT"}
    }
    # Reproduce the exact hardened pre-fix challenger prompt, not a new variant.
    paragraph = CHALLENGE_PROMPT.split("Report witness_correspondence:", 1)[1].split(
        "\n\nTest these questions", 1
    )[0]
    old_challenge = prompts["CHALLENGE_PROMPT"].replace(
        "reasoning or rescue it by silently changing the allegation or witness.\n",
        "reasoning or rescue it by silently changing the allegation or witness.\nReport witness_correspondence:"
        + paragraph
        + "\n",
    )
    approved = json.loads(
        (ROOT / "A_controlled_v7_live_v1_blocked_predispatch.json").read_text()
    )
    assert digest(old_challenge) == approved["prompt_sha256"]["challenge"]
    assert (
        digest(json.dumps(old.WireChallenge.model_json_schema(), sort_keys=True))
        == approved["schema_sha256"]["challenge"]
    )
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda s: len(enc.encode(s, disallowed_special=())),
    )

    raw_path = ROOT / "call_05.provider.json"
    raw = json.loads(raw_path.read_bytes())["choices"][0]["message"]["content"]
    legacy = old.WireAssessment.model_validate_json(raw).expanded()
    legacy_reply = json.loads((ROOT / "call_05.reply.json").read_text())
    assert legacy == AssessmentBatchOutput.model_validate_json(
        legacy_reply["output_json"]
    )
    legacy_request = AssessmentRequest.model_validate_json(
        (ROOT / "call_05.request.json").read_text()
    )
    try:
        WireAssessment.model_validate_json(raw)
    except ValidationError:
        new_contract_rejects_legacy = True
    else:
        raise AssertionError("Legacy opaque quote namespace must not be reinterpreted")
    evidence = {e.evidence_id: e for e in legacy_request.evidence}
    errors, literal_pairs = [], set()
    for proposal in legacy.units:
        unit = next(u for u in legacy_request.units if u.unit_id == proposal.unit_id)
        for issue in proposal.issues:
            try:
                validate_issue(
                    proposal, issue, legacy_request, unit, "unchanged-live-audit"
                )
            except ValueError as error:
                assert str(error) == "WRONG_REVISION_CITATION"
            else:
                raise AssertionError(
                    "Original bad-role allegation must remain rejected"
                )
            for role, refs in [
                ("PR_HEAD", issue.core.citations),
                *((r.revision, r.citations) for r in issue.revisions),
            ]:
                for ref in refs:
                    e = evidence[ref.evidence_id]
                    resolve_quote(ref, evidence)
                    literal_pairs.add((ref.evidence_id, ref.quote))
                    if e.kind != "CODE" or e.origin != role:
                        errors.append(
                            {
                                "required_role": role,
                                "actual_role": e.origin,
                                "evidence_id": e.evidence_id,
                                "quote": ref.quote,
                            }
                        )
            for ref in issue.rule.citations:
                resolve_quote(
                    QuoteRef(evidence_id=ref.evidence_id, quote=ref.quote), evidence
                )
                literal_pairs.add((ref.evidence_id, ref.quote))
    assert len(errors) == 6

    manifest = json.loads((ROOT / "manifest.json").read_text())
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    ctx = ArchitectureContext.model_validate_json(
        (ROOT / "A_unified_v7_certified_offline_context.json").read_text()
    )
    reader, _ = source_for(ctx.revision.repository_id, Path(case["source_root"]))
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    settings = EvaluationSettings.model_validate(manifest["settings"])
    model = CaseAScript()
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
    assessment, challenge = model.requests[0], model.challenge_requests[0]
    assert (
        compact(model.last_output, assessment).expanded(assessment) == model.last_output
    )
    sizing = ContextRequestBudget(
        reader, authority, settings=settings, token_counter=counter
    )
    full_ctx = ArchitectureContext.model_validate_json(
        (ROOT / "A_budget_preserved_v6_final_context.json").read_text()
    )
    await asyncio.to_thread(sizing, full_ctx)
    packets = [
        {
            "batch_id": p.batch_id,
            "unit_ids": [u.unit_id for u in p.units],
            "input_tokens": sizing.count(p, "ASSESS"),
            "maximum_output_tokens": settings.max_output_tokens,
            "fits": sizing.fits(p, "ASSESS"),
            "canonical_request_sha256": digest(p.model_dump_json()),
            "provider_payload_sha256": digest(provider_request_json(p, "ASSESS")),
        }
        for p in sizing.last_packets
    ]

    def old_wire_text(req, phase):
        schema = old.WireAssessment if phase == "ASSESS" else old.WireChallenge
        prompt = prompts["ASSESS_PROMPT"] if phase == "ASSESS" else old_challenge
        return (
            prompt
            + old.provider_request_json(req, phase)
            + json.dumps(
                schema.model_json_schema(), sort_keys=True, separators=(",", ":")
            )
        )

    metrics = {}
    for phase, req in (("ASSESS", assessment), ("CHALLENGE", challenge)):
        begin = time.perf_counter()
        for _ in range(10):
            wire_text(req, phase)
        elapsed = (time.perf_counter() - begin) * 100
        metrics[phase] = {
            "before_input_tokens": counter(old_wire_text(req, phase))
            + settings.provider_reserve,
            "after_input_tokens": sizing.count(req, phase),
            "max_output_tokens": settings.max_output_tokens,
            "fits": sizing.fits(req, phase),
            "mean_serializer_ms_10_iterations": elapsed,
        }
    assert metrics["ASSESS"]["before_input_tokens"] == 18921
    assert metrics["CHALLENGE"]["before_input_tokens"] == 18423
    for schema in (WireAssessment, WireChallenge):
        to_strict_json_schema(schema)
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
        "prompt_tokens": {
            "assessment": counter(ASSESS_PROMPT),
            "challenge": counter(CHALLENGE_PROMPT),
        },
        "measurements": metrics,
        "scripted_assessment_output_tokens_before": counter(
            old.compact(model.last_output).model_dump_json()
        ),
        "scripted_assessment_output_tokens_after": counter(
            compact(model.last_output, assessment).model_dump_json()
        ),
        "scoped_case_a": {
            "test_oracle": "scripted, not live accuracy",
            "outcome": result.outcome,
            "attribution": result.findings[0].attribution,
            "coverage": result.coverage.model_dump(mode="json"),
            "calls": [c.phase for c in result.calls],
        },
        "full_preserved_context_packets": packets,
        "intrinsic_omissions": [
            m.model_dump(mode="json")
            for m in sizing.last_unit_measurements
            if m.input_tokens > sizing.input_limit
        ],
        "retained_full_context_units": len(sizing.last_unit_measurements),
        "emitted_full_context_units": sum(len(p.units) for p in sizing.last_packets),
        "input_limit": settings.max_input_tokens,
        "output_limit": settings.max_output_tokens,
        "model_window": settings.model_window,
        "ledger_sha256": protected["aggregate_usage.json"],
        "historical_charged_tokens": 93485,
        "protected_artifacts_unchanged": len(protected),
        "stage41_source_files_unchanged": len(stage41),
    }
    assert all(p["fits"] for p in packets)
    assert all(file_hash(ROOT / name) == h for name, h in protected.items())
    assert all(file_hash(Path(p)) == h for p, h in stage41.items())
    save(
        "replay",
        {
            "legacy_expansion_exactly_matches_preserved_reply": True,
            "new_wire_contract_rejects_legacy": new_contract_rejects_legacy,
            "original_revision_errors": errors,
            "distinct_original_literal_pairs_checked": len(literal_pairs),
            "original_provider_sha256": file_hash(raw_path),
            "no_repair": True,
        },
    )
    save("result", result.model_dump(mode="json"))
    save("summary", summary)
    save(
        "integrity",
        {
            "protected_sha256": protected,
            "stage41_sha256": stage41,
            "all_unchanged": True,
            "billable_calls": 0,
        },
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "measurements",
                    "scripted_assessment_output_tokens_before",
                    "scripted_assessment_output_tokens_after",
                    "scoped_case_a",
                    "full_preserved_context_packets",
                    "intrinsic_omissions",
                    "protected_artifacts_unchanged",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
