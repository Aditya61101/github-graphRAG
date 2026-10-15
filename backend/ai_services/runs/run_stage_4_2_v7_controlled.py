"""One specifically authorized v7 Case A evaluation; no repeat dispatch path."""

import asyncio
import copy
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
from ai_services.decision_guard.evaluation.models import QuoteRef
from ai_services.decision_guard.evaluation.validation import (
    authority_established,
    source_coverage,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    provider_request_json,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.run_stage_4_2_live_validation import AggregateModel
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from openai.lib._pydantic import to_strict_json_schema

TAG = "controlled_v7_live_v2"
CEILING = 122658
RESERVATION = 48921


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def file_hash(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    with (ROOT / f"A_{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


async def run():
    if list(ROOT.glob(f"A_{TAG}_*.json")) or (ROOT / "call_05.request.json").exists():
        raise FileExistsError("Authorized attempt already recorded; never repeat")
    lease = ROOT / "live_validation.lock"
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    client = None
    started = time.monotonic()
    try:
        protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
        ledger = read("aggregate_usage.json")
        assert ledger["max_tokens"] == 77335
        assert len(ledger["calls"]) == 4
        assert sum(c["charged_tokens"] for c in ledger["calls"]) == 73737
        assert protected["aggregate_usage.json"] == (
            "d93c18585340f67013f2227f783f9555c758f7b0d4c0a7fd7fb18042fea12f6f"
        )
        approved = read("A_controlled_v7_live_v1_blocked_predispatch.json")
        assert PROMPT_VERSION == approved["prompt_version"] == "dg-semantic-v7"
        assert {
            "assessment": digest(ASSESS_PROMPT),
            "challenge": digest(CHALLENGE_PROMPT),
        } == approved["prompt_sha256"]
        assert {
            "assessment": digest(
                json.dumps(WireAssessment.model_json_schema(), sort_keys=True)
            ),
            "challenge": digest(
                json.dumps(WireChallenge.model_json_schema(), sort_keys=True)
            ),
        } == approved["schema_sha256"]
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
        assert (
            digest(provider_request_json(request, "ASSESS"))
            == approved["provider_payload_sha256"]
        )
        assert digest(wire_text(request, "ASSESS")) == approved["estimator_wire_sha256"]
        assert request.batch_id == approved["batch_id"]
        assert sizing.count(request, "ASSESS") == 18921
        assert settings.max_output_tokens == 5000 and settings.provider_reserve == 512
        assert sizing.count(request, "ASSESS") + 5000 == 23921
        assert sizing.input_limit + 5000 == 25000
        assert 23921 + 25000 == RESERVATION == CEILING - 73737
        assert sizing.fits(request, "ASSESS")
        assert all(source_coverage(request, u) for u in request.units)
        assert all(
            u.channel == "RKG" or authority_established(request, u)
            for u in request.units
        )
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
            prefix = ROOT / f"call_{4 + len(http_phases):02d}"
            with prefix.with_suffix(".provider.json").open("xb") as stream:
                stream.write(response.content)

        save(
            "authorization",
            {
                "aggregate_ceiling": CEILING,
                "historical_charged_tokens": 73737,
                "max_combined_reservation": RESERVATION,
                "max_assessments": 1,
                "max_challenges": 1,
                "retries": 0,
                "reassessments": 0,
                "request_sha256": approved["canonical_request_sha256"],
                "assessment_reservation": 23921,
                "challenge_upper_bound": 25000,
                "production_hashes": production_hashes,
                "protected_sha256": protected,
            },
        )
        save("previous_ledger", ledger)
        save("approved_request", request.model_dump(mode="json"))
        assert all(file_hash(ROOT / n) == h for n, h in protected.items())
        updated = copy.deepcopy(ledger)
        updated["max_tokens"] = CEILING
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
        aggregate = AggregateModel(adapter, ROOT, counter, max_tokens=CEILING)
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
                    assert req == request and reserve == 23921
                else:
                    assert reserve <= 25000
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
                    QuoteRef(evidence_id=citation.evidence_id, quote=citation.quote),
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
        assert current["calls"][:4] == ledger["calls"]
        assert len(current["calls"]) == 4 + len(phases)
        # A strict SDK parse failure can still have complete provider usage.
        # Reconcile only new entries using captured raw provider usage, never history.
        reconciled = []
        for index, entry in enumerate(current["calls"][4:], start=5):
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
            "new_ledger_entries": current["calls"][4:],
            "usage_reconciled_from_raw_calls": reconciled,
            "actual_new_input_tokens": sum(
                c.get("input_tokens", 0) or 0 for c in current["calls"][4:]
            ),
            "actual_new_output_tokens": sum(
                c.get("output_tokens", 0) or 0 for c in current["calls"][4:]
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
        raise SystemExit("Requires the specific v7 live-v2 authorization; never repeat")
    asyncio.run(run())
