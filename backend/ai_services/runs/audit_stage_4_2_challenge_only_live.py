"""Offline source/span, semantic witness and preservation audit of call_09."""

import ast
import hashlib
import json
from types import SimpleNamespace

from ai_services.decision_guard.evaluation.challenge import _catalog, resolve_span
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    ChallengeBatchOutput,
    ChallengeRequest,
    EvaluationResult,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import validate_review
from ai_services.decision_guard.evaluation.wire import (
    ROLE_PREFIX,
    NamedSpan,
    WireChallenge,
    source_references,
)
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT

TAG = "A_challenge_only_live_v1"


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def save(name, data):
    with (ROOT / f"{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


def run():
    request = ChallengeRequest.model_validate_json(
        (ROOT / "call_09.request.json").read_text()
    )
    provider = read("call_09.provider.json")
    raw = json.loads(provider["choices"][0]["message"]["content"])
    wire = WireChallenge.model_validate_json(json.dumps(raw))
    output = wire.expanded(request)
    assert output == ChallengeBatchOutput.model_validate_json(
        read("call_09.reply.json")["output_json"]
    )
    result = EvaluationResult.model_validate_json(
        (ROOT / f"{TAG}_result.json").read_text()
    )
    sources = source_references(request)
    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    catalog = _catalog(request)
    spans = []
    for item in raw["spans"]:
        span = NamedSpan.model_validate_json(json.dumps(item))
        source = sources[span.source_ref]
        assert span.span_id[0] == ROLE_PREFIX[span.source_ref.split(":")[0]]
        row = catalog[2][source.evidence_id]["excerpt_ref"]
        text = catalog[3][row["text_id"]]
        lines = text.splitlines(keepends=True)
        begin = sum(len(x) for x in lines[: span.start_line - 1]) + span.start_column
        end = sum(len(x) for x in lines[: span.end_line - 1]) + (
            len(lines[span.end_line - 1])
            if span.end_column is None
            else span.end_column
        )
        record = {
            **item,
            "evidence_id": source.evidence_id,
            "origin": source.origin,
            "commit_sha": source.commit_sha,
            "path": source.path,
            "excerpt_hash": source.excerpt_hash,
            "selected_characters": end - begin,
        }
        try:
            resolved = resolve_quote(resolve_span(span, request), evidence)
            record.update(valid=True, resolved=resolved.model_dump(mode="json"))
        except ValueError as error:
            assert str(error) == "SPAN_TOO_LARGE" and span.span_id == "A1"
            record.update(
                valid=False,
                reason=str(error),
                original_identity_and_bounds_valid=row["start_char"]
                <= begin
                < end
                <= row["end_char"],
                maximum_characters=1000,
            )
        spans.append(record)
    valid = {s["span_id"]: s for s in spans}
    uses = []

    def walk(value, role=None):
        if isinstance(value, dict):
            role = value.get("revision", role)
            for k, v in value.items():
                if k == "citations":
                    for key in v:
                        span = valid[key]
                        role_valid = role is None or span["origin"] == role
                        uses.append(
                            {
                                "span_id": key,
                                "required_role": role,
                                "actual_role": span["origin"],
                                "role_valid": role_valid,
                                "span_contract_valid": span["valid"],
                            }
                        )
                        assert role_valid
                else:
                    walk(v, role)
        elif isinstance(value, list):
            for v in value:
                walk(v, role)

    walk(raw["decisions"])
    reviewed = []
    for decision in output.decisions:
        candidate = next(
            c for c in request.candidates if c.proposal_id == decision.proposal_id
        )
        unit = next(
            u for u in request.assessment.units if u.unit_id == candidate.unit_id
        )
        accepted, citations, attribution = validate_review(
            decision, request.assessment, unit, candidate.proposal_id, candidate.issue
        )
        reviewed.append(
            {
                "finding_id": decision.proposal_id,
                "accepted": accepted,
                "verdict": decision.verdict,
                "attribution": attribution,
                "witness_correspondence": decision.witness_correspondence,
                "citation_count": len(citations),
            }
        )
    retained = []
    for subject in (*result.findings, *result.proofs):
        for citation in subject.citations:
            resolved = resolve_quote(
                QuoteRef(
                    evidence_id=citation.evidence_id,
                    quote=citation.quote,
                    start_char=citation.start_char,
                    end_char=citation.end_char,
                ),
                {e.evidence_id: e for e in result.evidence},
            )
            assert resolved == citation
            retained.append(
                {
                    "subject_id": getattr(subject, "finding_id", None)
                    or subject.proof_id,
                    "valid": True,
                    **citation.model_dump(mode="json"),
                }
            )

    # Independent offline check of the exact original mixed-status witness.
    # Executes only archived pure source, never a scripted model response, and
    # does not alter production decisions or manufacture a missing review.
    rows = [
        SimpleNamespace(status="VERIFIED", is_active=True),
        SimpleNamespace(status="EXTRACTED", is_active=True),
    ]

    class Query:
        def query(self, _):
            return self

        def filter(self, _):
            return self

        def all(self):
            return rows

    witness = {}
    for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD"):
        observation = next(
            o
            for o in request.assessment.observations
            if o.path.endswith("claim_status.py") and o.revision == role
        )
        original = evidence[observation.evidence_id]
        tree = ast.parse(original.excerpt)
        body = [
            n
            for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "derive_processing_status")
            or (
                isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "BLOCKING_STATUS"
                    for t in n.targets
                )
            )
        ]
        assert len(body) == 2
        namespace = {
            "Claim": object,
            "Session": object,
            "Document": SimpleNamespace(claim_id=0),
        }
        exec(  # noqa: S102 -- archived pure helper without imports
            compile(
                ast.Module(body=body, type_ignores=[]),
                "archived-original-witness-audit",
                "exec",
            ),
            namespace,
        )
        status = namespace["derive_processing_status"](SimpleNamespace(id=0), Query())[
            0
        ]
        witness[role] = {
            "status": status,
            "blocking_statuses": sorted(namespace["BLOCKING_STATUS"]),
            "witness_has_blocking_status": bool(
                {r.status for r in rows} & namespace["BLOCKING_STATUS"]
            ),
            "evidence_id": original.evidence_id,
        }
    assert (
        witness["MERGE_BASE"]["status"]
        == witness["TARGET_BASE"]["status"]
        == "DOCUMENT_PROCESSING"
    )
    assert witness["PR_HEAD"]["status"] == "READY_FOR_EVALUATION"
    authorization = read(f"{TAG}_authorization.json")
    for name, digest in authorization["protected_sha256"].items():
        if name != "aggregate_usage.json":
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    ledger = read("aggregate_usage.json")
    previous = read(f"{TAG}_previous_ledger.json")
    assert ledger["calls"][:8] == previous["calls"] and len(ledger["calls"]) == 9
    audit = {
        "span_definitions": spans,
        "span_uses": uses,
        "raw_decisions": [
            {
                "proposal_id": d["proposal_id"],
                "verdict": d["verdict"],
                "explanation": d["explanation"],
                "tested_witness": d["tested_witness"],
                "witness_correspondence": d["witness_correspondence"],
                "missing_facts": d["missing_facts"],
            }
            for d in raw["decisions"]
        ],
        "accepted_decisions": reviewed,
        "rejections": [r.model_dump(mode="json") for r in output.rejections],
        "span_definitions_valid": sum(s["valid"] for s in spans),
        "span_definitions_invalid": sum(not s["valid"] for s in spans),
        "wrong_revision_uses": 0,
        "source_fabrications": 0,
        "retained_citations_valid": len(retained),
        "witness_source_execution": witness,
        "actual_provider_usage": provider["usage"],
        "historical_files_unchanged": True,
        "historical_ledger_entries_unchanged": True,
    }
    save("structured_challenge", output.model_dump(mode="json"))
    save("source_span_audit", audit)
    save("citation_audit", retained)
    print(
        json.dumps(
            {
                "valid_spans": audit["span_definitions_valid"],
                "invalid_spans": audit["span_definitions_invalid"],
                "spans": [
                    {k: s[k] for k in ("span_id", "selected_characters", "valid")}
                    for s in spans
                ],
                "accepted": reviewed,
                "retained_citations": len(retained),
                "witness": witness,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    run()
