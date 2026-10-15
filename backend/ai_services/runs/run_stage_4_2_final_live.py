"""One authorized final hardened Case A evaluation; no repeat dispatch path."""

import asyncio
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.challenge import (
    expand_challenge_sources,
    resolve_span,
)
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
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
    AssessmentRequest,
    ChallengeRequest,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import (
    authority_established,
    source_coverage,
)
from ai_services.decision_guard.evaluation.wire import (
    ROLE_PREFIX,
    NamedSpan,
    WireAssessment,
    WireChallenge,
    expand_source_texts,
    provider_request_json,
    source_references,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.run_stage_4_2_live_validation import AggregateModel
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from openai.lib._pydantic import to_strict_json_schema

TAG = "final_hardened_live_v1"
CEILING = 179897
RESERVATION = 45082


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    with (ROOT / f"A_{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


class FinalAggregateModel(AggregateModel):
    """Honor this authorization's two calls after the seven preserved calls.

    Only local validation-ledger policy changes; production evaluation is intact.
    The outer phase guard additionally forbids retries, reassessment and call ten.
    """

    def __init__(self, model, directory, counter, *, max_tokens):
        self.model, self.directory, self.counter = model, Path(directory), counter
        self.max_tokens = max_tokens
        self.deployment_revision = model.deployment_revision
        self.path = self.directory / "aggregate_usage.json"
        self.lock = asyncio.Lock()
        self.case, self.run_tag = "", None
        self.state = json.loads(self.path.read_text(encoding="utf-8"))
        assert self.state["max_attempts"] == 9
        assert self.state["max_tokens"] == max_tokens == CEILING
        assert self.state["deployment_revision"] == self.deployment_revision
        assert len(self.state["calls"]) == 7

    @property
    def remaining_attempts(self):
        return max(0, 9 - len(self.state["calls"]))


async def run():
    if list(ROOT.glob(f"A_{TAG}_*.json")) or (ROOT / "call_08.request.json").exists():
        raise FileExistsError("Authorized attempt already recorded; never repeat")
    lease = ROOT / "live_validation.lock"
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    client = None
    started = time.monotonic()
    try:
        protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
        ledger = read("aggregate_usage.json")
        assert ledger["max_tokens"] == 142658
        assert len(ledger["calls"]) == 7
        assert ledger["max_attempts"] == 8
        assert sum(c["charged_tokens"] for c in ledger["calls"]) == 134815
        assert protected["aggregate_usage.json"] == (
            "2678587996b5936e351fe99f524b90cb1525e41c0cf4b5aa43e1280110f03a40"
        )
        approved = read("A_controlled_v7_live_v1_blocked_predispatch.json")
        contract = read("A_challenger_hardened_offline_summary.json")
        assert (
            PROMPT_VERSION
            == contract["prompt_version"]
            == "dg-semantic-v7-challenger-v1"
        )
        assert {
            "assessment": digest(ASSESS_PROMPT),
            "challenge": digest(CHALLENGE_PROMPT),
        } == contract["prompt_hashes"]
        assert {
            "assessment": digest(
                json.dumps(WireAssessment.model_json_schema(), sort_keys=True)
            ),
            "challenge": digest(
                json.dumps(WireChallenge.model_json_schema(), sort_keys=True)
            ),
        } == contract["schema_hashes"]
        manifest = read("manifest.json")
        case = next(c for c in manifest["cases"] if c["id"] == "A")
        ctx = validate_context(
            ArchitectureContext.model_validate_json(
                (ROOT / approved["context_file"]).read_text(encoding="utf-8")
            )
        )
        assert ctx.context_hash == approved["context_hash"]
        assert ctx.revision.model_dump(mode="json") == approved["revision"]
        source, _ = source_for(ctx.revision.repository_id, Path(case["source_root"]))
        authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
        settings = EvaluationSettings.model_validate(manifest["settings"]).model_copy(
            update={
                "max_attempts": 2,
                "max_tokens": RESERVATION,
                "retries": 0,
                "concurrency": 1,
            }
        )
        enc = tiktoken.get_encoding("o200k_base")
        counter = ModelTokenCounter(
            "o200k_base:deployment-estimate",
            lambda text: len(enc.encode(text, disallowed_special=())),
        )
        sizing = ContextRequestBudget(
            source, authority, settings=settings, token_counter=counter
        )
        await asyncio.to_thread(sizing, ctx)
        assert len(sizing.last_packets) == 1
        request = sizing.last_packets[0]
        assert digest(request.model_dump_json()) == approved["canonical_request_sha256"]
        frozen = AssessmentRequest.model_validate_json(
            (ROOT / "call_05.request.json").read_text(encoding="utf-8")
        )
        assert request == frozen
        expected_payload = provider_request_json(frozen, "ASSESS")
        assert provider_request_json(request, "ASSESS") == expected_payload
        assert (
            digest(expected_payload)
            == contract["input_metrics"]["assessment"]["provider_payload_sha256"]
        )
        assert wire_text(request, "ASSESS") == wire_text(frozen, "ASSESS")
        restored = expand_source_texts(json.loads(expected_payload))
        assert restored["evidence"] == [
            e.model_dump(mode="json") for e in frozen.evidence
        ]
        refs = source_references(request)
        assert len(refs) == len(set(refs))
        for ref, evidence in refs.items():
            if evidence.kind == "CODE":
                assert ref.startswith(evidence.origin + ":")
                assert evidence.origin in {"MERGE_BASE", "TARGET_BASE", "PR_HEAD"}
        assert request.batch_id == approved["batch_id"]
        assert sizing.count(request, "ASSESS") == 19173
        assert settings.max_output_tokens == 5000 and settings.provider_reserve == 512
        assert sizing.count(request, "ASSESS") + 5000 == 24173
        assert sizing.input_limit + 5000 == 25000
        assert 24173 + 20909 == RESERVATION == CEILING - 134815
        assert sizing.fits(request, "ASSESS")
        assert all(source_coverage(request, u) for u in request.units)
        assert all(
            u.channel == "RKG" or authority_established(request, u)
            for u in request.units
        )
        # Verify the authorized joint-review sizing using the exact preserved
        # source-valid candidates; live candidates are allowed to differ but must
        # independently fit the same upper reservation before their dispatch.
        reference_challenge = ChallengeRequest.model_validate_json(
            (ROOT / "A_challenger_hardened_offline_combined_request.json").read_text(
                encoding="utf-8"
            )
        )
        reference_payload = provider_request_json(reference_challenge, "CHALLENGE")
        reference_metrics = contract["input_metrics"]["both_historical_candidates"]
        assert (
            digest(reference_challenge.model_dump_json())
            == reference_metrics["canonical_request_sha256"]
        )
        assert digest(reference_payload) == reference_metrics["provider_payload_sha256"]
        assert sizing.count(reference_challenge, "CHALLENGE") == 15909
        assert sizing.count(reference_challenge, "CHALLENGE") + 5000 == 20909
        restored_review = expand_challenge_sources(json.loads(reference_payload))
        original_review = {
            e.evidence_id: e for e in reference_challenge.assessment.evidence
        }
        assert all(e == original_review[e.evidence_id] for e in restored_review)
        span_boundary_audit = []
        projection = json.loads(reference_payload)
        for row in projection["sources"]:
            if not row.get("source_ref") or not row.get("span_bounds"):
                continue
            bounds = row["span_bounds"]
            span = NamedSpan(
                span_id=ROLE_PREFIX[row["source_ref"].split(":")[0]] + "1",
                source_ref=row["source_ref"],
                start_line=bounds["start_line"],
                end_line=bounds["start_line"],
                start_column=bounds["start_column"],
                end_column=bounds["start_column"] + 1,
            )
            ref = resolve_span(span, reference_challenge)
            resolve_quote(ref, original_review)
            span_boundary_audit.append(
                {
                    "source_ref": row["source_ref"],
                    "evidence_id": ref.evidence_id,
                    "span_bounds": bounds,
                    "verified": True,
                }
            )
        assert len(span_boundary_audit) == len(source_references(reference_challenge))
        for schema in (WireAssessment, WireChallenge):
            to_strict_json_schema(schema)
        load_dotenv(".env")
        assert (
            os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
            == manifest["deployment"]
            == "aicloud-gpt-5.4"
        )
        assert os.environ["AZURE_OPENAI_API_VERSION"] == manifest["api_version"]
        assert (
            ledger["deployment_revision"]
            == "aicloud-gpt-5.4:configured:2024-12-01-preview"
        )
        module_root = Path(__file__).parents[1] / "decision_guard"
        production_hashes = {str(p): file_hash(p) for p in module_root.rglob("*.py")}
        phases, http_phases, reservations, denied = [], [], [], []
        active = {}

        async def guard(http_request):
            phase = active["phase"]
            assert len(http_phases) < len(phases) <= 2
            assert phase == ("ASSESS" if not http_phases else "CHALLENGE")
            assert http_request.method == "POST" and http_request.url.path.endswith(
                "/chat/completions"
            )
            payload = json.loads(http_request.content)
            assert payload["model"] == manifest["deployment"]
            assert payload["max_completion_tokens"] == 5000
            assert payload["messages"] == [
                {
                    "role": "system",
                    "content": ASSESS_PROMPT if phase == "ASSESS" else CHALLENGE_PROMPT,
                },
                {
                    "role": "user",
                    "content": provider_request_json(active["request"], phase),
                },
            ]
            schema = WireAssessment if phase == "ASSESS" else WireChallenge
            assert payload["response_format"]["json_schema"][
                "schema"
            ] == to_strict_json_schema(schema)
            assert all(file_hash(Path(p)) == h for p, h in production_hashes.items())
            http_phases.append(phase)
            save(f"http_{len(http_phases)}", {"phase": phase, "payload": payload})

        async def capture(response):
            await response.aread()
            prefix = ROOT / f"call_{7 + len(http_phases):02d}"
            with prefix.with_suffix(".provider.json").open("xb") as stream:
                stream.write(response.content)

        save(
            "authorization",
            {
                "aggregate_ceiling": CEILING,
                "historical_charged_tokens": 134815,
                "max_combined_reservation": RESERVATION,
                "max_assessments": 1,
                "max_challenges": 1,
                "retries": 0,
                "reassessments": 0,
                "request_sha256": approved["canonical_request_sha256"],
                "provider_payload_sha256": digest(expected_payload),
                "estimator_wire_sha256": digest(wire_text(request, "ASSESS")),
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": contract["prompt_hashes"],
                "schema_sha256": contract["schema_hashes"],
                "source_handles": {ref: e.evidence_id for ref, e in refs.items()},
                "assessment_reservation": 24173,
                "challenge_upper_bound": 20909,
                "aggregate_attempt_ceiling": 9,
                "attempt_authorization": "Seven historical calls plus this instruction's one assessment and at most one challenge",
                "reference_challenge_input": 15909,
                "reference_challenge_sha256": digest(
                    reference_challenge.model_dump_json()
                ),
                "source_span_boundary_audit": span_boundary_audit,
                "production_hashes": production_hashes,
                "protected_sha256": protected,
            },
        )
        save("previous_ledger", ledger)
        save("approved_request", request.model_dump(mode="json"))
        assert all(file_hash(ROOT / n) == h for n, h in protected.items())
        updated = copy.deepcopy(ledger)
        updated["max_tokens"] = CEILING
        updated["max_attempts"] = 9
        temporary = ROOT / "aggregate_usage.tmp"
        temporary.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        temporary.replace(ROOT / "aggregate_usage.json")
        client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=manifest["api_version"],
            max_retries=0,
            timeout=settings.call_timeout_seconds,
            http_client=httpx.AsyncClient(
                event_hooks={"request": [guard], "response": [capture]}
            ),
        )
        adapter = AzureEvaluationModel(
            SimpleNamespace(client=client, deployment=manifest["deployment"]),
            deployment_revision=ledger["deployment_revision"],
        )
        aggregate = FinalAggregateModel(adapter, ROOT, counter, max_tokens=CEILING)
        aggregate.case, aggregate.run_tag = "A", TAG

        class OneEvaluation:
            deployment_revision = aggregate.deployment_revision

            async def invoke(self, req, phase, **kwargs):
                expected = "ASSESS" if not phases else "CHALLENGE"
                if len(phases) >= 2 or phase != expected:
                    denied.append(phase)
                    raise RuntimeError(
                        "Unauthorized reassessment/additional call blocked"
                    )
                reserve = sizing.count(req, phase) + kwargs["max_output_tokens"]
                assert sizing.fits(req, phase) and kwargs["max_output_tokens"] == 5000
                if phase == "ASSESS":
                    assert req == request and reserve == 24173
                else:
                    if reserve > 20909 or sum(reservations) + reserve > RESERVATION:
                        save(
                            "challenge_blocked",
                            {
                                "input_estimate": sizing.count(req, phase),
                                "reservation": reserve,
                                "remaining_authorized_reservation": RESERVATION
                                - sum(reservations),
                                "reason": "LIVE_CHALLENGE_EXCEEDS_AUTHORIZED_RESERVATION",
                                "model_call_dispatched": False,
                            },
                        )
                    assert reserve <= 20909
                    live_projection = json.loads(
                        provider_request_json(req, "CHALLENGE")
                    )
                    original = {e.evidence_id: e for e in req.assessment.evidence}
                    assert all(
                        original[e.evidence_id] == e
                        for e in expand_challenge_sources(live_projection)
                    )
                assert sum(reservations) + reserve <= RESERVATION
                assert all(
                    file_hash(ROOT / n) == h
                    for n, h in protected.items()
                    if n != "aggregate_usage.json"
                )
                phases.append(phase)
                reservations.append(reserve)
                active.update(phase=phase, request=req)
                save(
                    f"{phase.lower()}_dispatch",
                    {
                        "phase": phase,
                        "reservation": reserve,
                        "input_estimate": sizing.count(req, phase),
                    },
                )
                return await (
                    aggregate.assess if phase == "ASSESS" else aggregate.challenge
                )(req, **kwargs)

            async def assess(self, req, **kwargs):
                return await self.invoke(req, "ASSESS", **kwargs)

            async def challenge(self, req, **kwargs):
                return await self.invoke(req, "CHALLENGE", **kwargs)

        evaluation_started = time.monotonic()
        result = await DecisionIntegrityEvaluator(
            OneEvaluation(),
            source=source,
            authority=authority,
            settings=settings,
            token_counter=counter,
        ).evaluate(ctx)
        save("result", result.model_dump(mode="json"))
        citation_audit = []
        for item in (*result.findings, *result.proofs):
            for citation in item.citations:
                resolved = resolve_quote(
                    QuoteRef(
                        evidence_id=citation.evidence_id,
                        quote=citation.quote,
                        start_char=citation.start_char,
                        end_char=citation.end_char,
                    ),
                    {e.evidence_id: e for e in result.evidence},
                )
                citation_audit.append(
                    {
                        "subject": getattr(item, "finding_id", None) or item.proof_id,
                        "valid": resolved == citation,
                        **citation.model_dump(mode="json"),
                    }
                )
        current = read("aggregate_usage.json")
        assert current["calls"][:7] == ledger["calls"]
        assert len(current["calls"]) == 7 + len(phases)
        # A strict SDK parse failure can still have complete provider usage.
        # Reconcile only new entries using captured raw provider usage, never history.
        reconciled = []
        for index, entry in enumerate(current["calls"][7:], start=8):
            raw_path = ROOT / f"call_{index:02d}.provider.json"
            if raw_path.exists():
                raw = json.loads(raw_path.read_bytes())
                usage = raw.get("usage")
                if usage and entry.get("input_tokens") is None:
                    entry.update(
                        input_tokens=usage["prompt_tokens"],
                        output_tokens=usage["completion_tokens"],
                        charged_tokens=usage["total_tokens"],
                        request_id=raw.get("id"),
                    )
                    reconciled.append(index)
        if reconciled:
            temporary.write_text(json.dumps(current, indent=2), encoding="utf-8")
            temporary.replace(ROOT / "aggregate_usage.json")
        unchanged = {
            n: file_hash(ROOT / n) == h
            for n, h in protected.items()
            if n != "aggregate_usage.json"
        }
        assert all(unchanged.values())
        assert all(file_hash(Path(p)) == h for p, h in production_hashes.items())
        summary = {
            "outcome": result.outcome,
            "execution": result.execution,
            "coverage": result.coverage.model_dump(mode="json"),
            "findings": [
                {
                    "disposition": f.disposition,
                    "attribution": f.attribution,
                    "claim": f.claim,
                    "review_error": f.review_error,
                    "review": f.review.model_dump(mode="json") if f.review else None,
                }
                for f in result.findings
            ],
            "unit_statuses": [u.model_dump(mode="json") for u in result.units],
            "http_phases": http_phases,
            "denied_extra_calls": denied,
            "reservations": reservations,
            "combined_reservation": sum(reservations),
            "new_ledger_entries": current["calls"][7:],
            "usage_reconciled_from_raw_calls": reconciled,
            "actual_new_input_tokens": sum(
                c.get("input_tokens", 0) or 0 for c in current["calls"][7:]
            ),
            "actual_new_output_tokens": sum(
                c.get("output_tokens", 0) or 0 for c in current["calls"][7:]
            ),
            "aggregate_charged_tokens": sum(
                c["charged_tokens"] for c in current["calls"]
            ),
            "remaining_tokens": CEILING
            - sum(c["charged_tokens"] for c in current["calls"]),
            "evaluation_latency_seconds": time.monotonic() - evaluation_started,
            "runner_latency_seconds": time.monotonic() - started,
            "citation_count": len(citation_audit),
            "all_citations_valid": all(c["valid"] for c in citation_audit),
            "rejected_findings": [
                r.model_dump(mode="json") for r in result.rejected_findings
            ],
            "unselected_units_in_larger_context": approved[
                "other_full_context_units_not_selected"
            ],
            "historical_ledger_entries_unchanged": True,
        }
        save("citation_audit", citation_audit)
        save("summary", summary)
        save(
            "integrity",
            {
                "historical_files_unchanged": unchanged,
                "historical_ledger_entries_unchanged": True,
                "new_ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
                "production_source_unchanged": True,
            },
        )
        print(
            json.dumps(
                {
                    k: summary[k]
                    for k in (
                        "outcome",
                        "coverage",
                        "http_phases",
                        "reservations",
                        "actual_new_input_tokens",
                        "actual_new_output_tokens",
                        "aggregate_charged_tokens",
                        "evaluation_latency_seconds",
                    )
                },
                indent=2,
            )
        )
    except BaseException as error:
        if not (ROOT / f"A_{TAG}_failure.json").exists():
            save(
                "failure",
                {
                    "error_type": type(error).__name__,
                    "runner_latency_seconds": time.monotonic() - started,
                    "repeat_forbidden": True,
                },
            )
        raise
    finally:
        if client is not None:
            await client.close()
        lease.unlink(missing_ok=True)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--execute-authorized-once"]:
        raise SystemExit(
            "Requires the specific final hardened live-v1 authorization; never repeat"
        )
    asyncio.run(run())
