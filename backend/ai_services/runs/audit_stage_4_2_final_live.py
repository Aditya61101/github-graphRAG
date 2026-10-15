"""Post-run, offline-only audit of the final hardened live assessment.

Never dispatches a model request or edits original results/usage entries.
"""

import asyncio
import json
from pathlib import Path

import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import (
    Pending,
    RequestBudget,
    build_units,
    plan_challenges,
    prepare_evaluation_evidence,
)
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    EvaluationResult,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_issue,
    validate_proposal,
)
from ai_services.decision_guard.evaluation.wire import (
    ROLE_PREFIX,
    provider_request_json,
    source_references,
)
from ai_services.decision_guard.models import ArchitectureContext, digest, stable_id
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

TAG = "A_final_hardened_live_v1"


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def save(name, data):
    with (ROOT / f"{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


async def run():
    request = AssessmentRequest.model_validate_json(
        (ROOT / "call_08.request.json").read_text()
    )
    reply = read("call_08.reply.json")
    output = AssessmentBatchOutput.model_validate_json(reply["output_json"])
    raw = read("call_08.provider.json")
    wire = json.loads(raw["choices"][0]["message"]["content"])
    result = EvaluationResult.model_validate_json(
        (ROOT / f"{TAG}_result.json").read_text()
    )
    sources = source_references(request)
    evidence = {e.evidence_id: e for e in request.evidence}
    definitions, quote_audit, usages = {}, [], []
    for item in wire["quotes"]:
        e = sources[item["source_ref"]]
        citation = resolve_quote(
            QuoteRef(evidence_id=e.evidence_id, quote=item["quote"]), evidence
        )
        assert item["quote_id"][0] == ROLE_PREFIX[item["source_ref"].split(":")[0]]
        definitions[item["quote_id"]] = e
        quote_audit.append(
            {
                **item,
                "valid": True,
                "evidence_id": e.evidence_id,
                "origin": e.origin,
                "commit_sha": e.commit_sha,
                "resolved": citation.model_dump(mode="json"),
            }
        )

    def walk(value, role=None):
        if isinstance(value, dict):
            role = value.get("revision", role)
            for k, v in value.items():
                if k in {"support", "citations"}:
                    for q in v:
                        e = definitions[q]
                        valid = role is None or (e.kind == "CODE" and e.origin == role)
                        usages.append(
                            {
                                "quote_id": q,
                                "required_role": role,
                                "actual_role": e.origin,
                                "valid": valid,
                            }
                        )
                        assert valid
                else:
                    walk(v, "PR_HEAD" if k == "core" else role)
        elif isinstance(value, list):
            for v in value:
                walk(v, role)

    walk(wire["units"])

    approved = read("A_controlled_v7_live_v1_blocked_predispatch.json")
    ctx = ArchitectureContext.model_validate_json(
        (ROOT / approved["context_file"]).read_text()
    )
    manifest = read("manifest.json")
    source, _ = source_for(
        ctx.revision.repository_id,
        Path(next(c["source_root"] for c in manifest["cases"] if c["id"] == "A")),
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    settings = EvaluationSettings.model_validate(manifest["settings"]).model_copy(
        update={"max_attempts": 2, "max_tokens": 45082, "retries": 0, "concurrency": 1}
    )
    receipts, _, _, bundle, _ = await prepare_evaluation_evidence(
        ctx, settings, source, authority
    )
    by_unit = {p.unit_id: p for p in output.units}
    items, validations = [], []
    for original in build_units(ctx, settings.max_units)[0]:
        unit = next(u for u in request.units if u.unit_id == original.unit_id)
        proposal = by_unit[unit.unit_id]
        validate_proposal(proposal, request, unit)
        for index, issue in enumerate(proposal.issues):
            key = stable_id(unit.unit_id, str(index), issue.claim)
            proofs, cites, attribution, eligible = validate_issue(
                proposal, issue, request, unit, key
            )
            items.append(
                Pending(
                    unit, proposal, issue, key, proofs, cites, attribution, eligible
                )
            )
            validations.append(
                {
                    "finding_id": key,
                    "unit_id": unit.unit_id,
                    "channel": unit.channel,
                    "source_valid": True,
                    "eligible": eligible,
                    "attribution": attribution,
                    "applicability": proposal.applicability,
                    "normative": proposal.normative,
                    "lifecycle": proposal.lifecycle,
                    "core": issue.core.model_dump(mode="json"),
                    "counterevidence": issue.counterevidence.model_dump(mode="json"),
                    "missing_facts": [
                        f.model_dump(mode="json") for f in issue.missing_facts
                    ],
                }
            )
    assert len(items) == 2 and all(p.eligible for p in items)
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda s: len(enc.encode(s, disallowed_special=())),
    )
    budget = RequestBudget(settings, counter)
    packets, omitted = plan_challenges(ctx, items, bundle, receipts, budget)
    assert len(packets) == 1 and not omitted
    challenge = packets[0]
    assert challenge.batch_id == result.calls[1].batch_id
    assert budget.count(challenge, "CHALLENGE") == 16019
    assert (
        not (ROOT / "call_09.request.json").exists()
        and not (ROOT / "call_09.provider.json").exists()
    )
    diagnostics = {
        "eligible_candidate_count": 2,
        "planned_packet_count": 1,
        "candidate_ids": [c.proposal_id for c in challenge.candidates],
        "input_estimate": 16019,
        "maximum_output_tokens": 5000,
        "reservation": 21019,
        "fits_configured_input_ceiling": True,
        "configured_input_ceiling": budget.input_limit,
        "authorized_remaining_reservation": 20909,
        "combined_if_dispatched": 45192,
        "authorized_combined_reservation": 45082,
        "excess_tokens": 110,
        "api_challenge_requests": 0,
        "span_response_received": False,
        "independent_verdicts": [],
        "reason": "LIVE_CHALLENGE_EXCEEDS_AUTHORIZED_RESERVATION",
        "engine_records_local_guard_as": "MODEL_ERROR / REVIEW_FAILED",
        "not_a_provider_or_span_failure": True,
        "original_candidate_retained": all(
            f.candidate is not None for f in result.findings
        ),
        "production_result_reserved_unreported_tokens": result.reserved_unreported_tokens,
        "reserved_unreported_explanation": "The evaluator conservatively retains its 21,019 challenge reservation because the local authorization guard raised before HTTP dispatch. No challenge ledger entry, provider request or provider usage exists.",
        "blocked_request_sha256": digest(challenge.model_dump_json()),
        "blocked_projection_sha256": digest(
            provider_request_json(challenge, "CHALLENGE")
        ),
    }
    save("structured_assessment", output.model_dump(mode="json"))
    save("source_validation", validations)
    save(
        "full_response_source_audit",
        {
            "quote_definitions": quote_audit,
            "citation_uses": usages,
            "all_valid": True,
            "wrong_revision_uses": 0,
            "actual_provider_usage": raw["usage"],
        },
    )
    save("blocked_challenge_request", challenge.model_dump(mode="json"))
    save(
        "blocked_challenge_projection",
        json.loads(provider_request_json(challenge, "CHALLENGE")),
    )
    save("challenge_diagnostics", diagnostics)
    print(
        json.dumps(
            {
                "quotes": len(quote_audit),
                "citation_uses": len(usages),
                "candidates": validations,
                "challenge": diagnostics,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
