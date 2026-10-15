"""One specifically authorized challenge, reusing authentic call_08 assessment.

No assessment dispatch path. Production evaluator performs all source/review
validation and final aggregation; preserved artifacts and ledger are never reset.
"""

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
)
from ai_services.decision_guard.evaluation.engine import (
    Pending,
    RequestBudget,
    build_units,
    make_request,
    plan_challenges,
    prepare_evaluation_evidence,
)
from ai_services.decision_guard.evaluation.evidence import (
    validate_context,
)
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
    PROMPT_VERSION,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    ChallengeRequest,
    DispatchDenied,
    EvaluationResult,
    ModelReply,
    PreservedAssessment,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_issue,
    validate_proposal,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    provider_request_json,
)
from ai_services.decision_guard.models import ArchitectureContext, digest, stable_id
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.run_stage_4_2_live_validation import AggregateModel
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from openai.lib._pydantic import to_strict_json_schema

TAG = "challenge_only_live_v1"
CEILING = 180885
UPPER_RESERVATION = 25000
REQUEST_HASH = "1209022bc31f13f8a30e91da3f006c1290decec6bbe4ea3ddc0878e94eea7654"
PAYLOAD_HASH = "44599d937378acecd2e1ccb811b60dd0d3b555e5b26822f48d8662c908d0adfb"


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, data):
    with (ROOT / f"A_{TAG}_{name}.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)


class ChallengeLedger(AggregateModel):
    def __init__(self, model, counter):
        self.model, self.directory, self.counter = model, ROOT, counter
        self.max_tokens = CEILING
        self.deployment_revision = model.deployment_revision
        self.path = ROOT / "aggregate_usage.json"
        self.lock = asyncio.Lock()
        self.case, self.run_tag = "A", TAG
        self.state = read("aggregate_usage.json")
        assert self.state["max_tokens"] == CEILING and self.state["max_attempts"] == 9
        assert (
            len(self.state["calls"]) == 8
            and self.state["deployment_revision"] == self.deployment_revision
        )

    @property
    def remaining_attempts(self):
        return max(0, 9 - len(self.state["calls"]))


async def run():
    if (
        list(ROOT.glob(f"A_{TAG}_*.json"))
        or (ROOT / "call_09.request.json").exists()
        or (ROOT / "call_09.provider.json").exists()
    ):
        raise FileExistsError("Challenge-only attempt already recorded; never repeat")
    lease = ROOT / "live_validation.lock"
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    client = None
    started = time.monotonic()
    try:
        protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
        previous = read("aggregate_usage.json")
        assert previous["max_tokens"] == 179897 and previous["max_attempts"] == 9
        assert (
            len(previous["calls"]) == 8
            and sum(c["charged_tokens"] for c in previous["calls"]) == 155885
        )
        assert (
            file_hash(ROOT / "aggregate_usage.json")
            == "6d762f1a263fc92b3fb8066c2c676516f217e775ae9ea14444ab9d44ee6831df"
        )
        assert CEILING - 155885 == UPPER_RESERVATION
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
        to_strict_json_schema(WireChallenge)
        old_authorization = read("A_final_hardened_live_v1_authorization.json")
        changed = [
            p
            for p, h in old_authorization["production_hashes"].items()
            if file_hash(p) != h
        ]
        assert {Path(p).name for p in changed} == {"models.py", "engine.py"}
        assert all("evaluation" in Path(p).parts for p in changed)
        approved = read("A_controlled_v7_live_v1_blocked_predispatch.json")
        ctx = validate_context(
            ArchitectureContext.model_validate_json(
                (ROOT / approved["context_file"]).read_text()
            )
        )
        assert (
            ctx.context_hash == approved["context_hash"]
            and ctx.revision.model_dump(mode="json") == approved["revision"]
        )
        original = AssessmentRequest.model_validate_json(
            (ROOT / "call_08.request.json").read_text()
        )
        reply = ModelReply.model_validate_json(
            (ROOT / "call_08.reply.json").read_text()
        )
        raw = read("call_08.provider.json")
        parsed = WireAssessment.model_validate_json(
            raw["choices"][0]["message"]["content"]
        )
        output = parsed.expanded(original)
        assert output == AssessmentBatchOutput.model_validate_json(reply.output_json)
        assert parsed == WireAssessment.model_validate_json(reply.wire_output_json)
        assert reply.request_id == raw["id"] and (
            reply.input_tokens,
            reply.output_tokens,
        ) == (raw["usage"]["prompt_tokens"], raw["usage"]["completion_tokens"]) == (
            18050,
            3020,
        )
        assert (
            reply.status == "OK"
            and digest(original.model_dump_json())
            == approved["canonical_request_sha256"]
        )
        historical_result = EvaluationResult.model_validate_json(
            (ROOT / "A_final_hardened_live_v1_result.json").read_text()
        )
        assert {f.candidate for f in historical_result.findings} == {
            i for p in output.units for i in p.issues
        }
        manifest = read("manifest.json")
        case = next(c for c in manifest["cases"] if c["id"] == "A")
        source, _ = source_for(ctx.revision.repository_id, Path(case["source_root"]))
        authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
        settings = EvaluationSettings.model_validate(manifest["settings"]).model_copy(
            update={
                "max_attempts": 1,
                "max_tokens": UPPER_RESERVATION,
                "retries": 0,
                "concurrency": 1,
            }
        )
        enc = tiktoken.get_encoding("o200k_base")
        counter = ModelTokenCounter(
            "o200k_base:deployment-estimate",
            lambda s: len(enc.encode(s, disallowed_special=())),
        )
        budget = RequestBudget(settings, counter)
        receipts, _, _, bundle, _ = await prepare_evaluation_evidence(
            ctx, settings, source, authority
        )
        units = build_units(ctx, settings.max_units)[0]
        current = make_request(ctx, units, bundle, receipts)
        assert current == original
        proposals = {p.unit_id: p for p in output.units}
        pending = []
        for unit in current.units:
            proposal = proposals[unit.unit_id]
            validate_proposal(proposal, current, unit)
            for index, issue in enumerate(proposal.issues):
                key = stable_id(unit.unit_id, str(index), issue.claim)
                proofs, citations, attribution, eligible = validate_issue(
                    proposal, issue, current, unit, key
                )
                assert eligible
                pending.append(
                    Pending(
                        unit,
                        proposal,
                        issue,
                        key,
                        proofs,
                        citations,
                        attribution,
                        eligible,
                    )
                )
        packets, omitted = plan_challenges(ctx, pending, bundle, receipts, budget)
        assert len(pending) == 2 and len(packets) == 1 and not omitted
        request = packets[0]
        blocked = ChallengeRequest.model_validate_json(
            (
                ROOT / "A_final_hardened_live_v1_blocked_challenge_request.json"
            ).read_text()
        )
        assert request == blocked and digest(request.model_dump_json()) == REQUEST_HASH
        payload = provider_request_json(request, "CHALLENGE")
        assert digest(payload) == PAYLOAD_HASH
        assert {c.proposal_id for c in request.candidates} == {
            f.finding_id for f in historical_result.findings
        }
        assert (
            budget.count(request, "CHALLENGE") == 16019
            and settings.max_output_tokens == 5000
            and budget.input_limit == 20000
        )
        reserve = budget.count(request, "CHALLENGE") + 5000
        assert reserve == 21019 <= UPPER_RESERVATION and budget.fits(
            request, "CHALLENGE"
        )
        original_sources = {e.evidence_id: e for e in request.assessment.evidence}
        assert all(
            original_sources[e.evidence_id] == e
            for e in expand_challenge_sources(json.loads(payload))
        )
        preserved = PreservedAssessment(
            request=original,
            reply=reply,
            provider_response_sha256=file_hash(ROOT / "call_08.provider.json"),
        )
        load_dotenv(".env")
        assert (
            os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
            == manifest["deployment"]
            == "aicloud-gpt-5.4"
        )
        assert os.environ["AZURE_OPENAI_API_VERSION"] == manifest["api_version"]
        module_root = Path(__file__).parents[1] / "decision_guard"
        production_hashes = {str(p): file_hash(p) for p in module_root.rglob("*.py")}
        phases = []
        invoked = []

        def verify_live_state():
            assert all(file_hash(Path(p)) == h for p, h in production_hashes.items())
            assert all(
                file_hash(ROOT / n) == h
                for n, h in protected.items()
                if n != "aggregate_usage.json"
            )

        async def guard(http_request):
            assert invoked == ["CHALLENGE"] and not phases
            verify_live_state()
            body = json.loads(http_request.content)
            assert http_request.method == "POST" and http_request.url.path.endswith(
                "/chat/completions"
            )
            assert (
                body["model"] == manifest["deployment"]
                and body["max_completion_tokens"] == 5000
            )
            assert body["messages"] == [
                {"role": "system", "content": CHALLENGE_PROMPT},
                {"role": "user", "content": payload},
            ]
            assert body["response_format"]["json_schema"][
                "schema"
            ] == to_strict_json_schema(WireChallenge)
            save("http_1", {"phase": "CHALLENGE", "payload": body})
            phases.append("CHALLENGE")

        async def capture(response):
            await response.aread()
            with (ROOT / "call_09.provider.json").open("xb") as stream:
                stream.write(response.content)

        save(
            "authorization",
            {
                "ceiling": CEILING,
                "historical_charged_tokens": 155885,
                "maximum_call_reservation": UPPER_RESERVATION,
                "exact_reservation": reserve,
                "input_estimate": 16019,
                "max_output": 5000,
                "prompt_version": PROMPT_VERSION,
                "prompt_hashes": contract["prompt_hashes"],
                "schema_hashes": contract["schema_hashes"],
                "request_sha256": REQUEST_HASH,
                "payload_sha256": PAYLOAD_HASH,
                "candidate_ids": [c.proposal_id for c in request.candidates],
                "assessment_provider_response_sha256": preserved.provider_response_sha256,
                "assessment_request_sha256": digest(original.model_dump_json()),
                "protected_sha256": protected,
                "production_hashes": production_hashes,
                "permitted_diagnostic_code_changes": changed,
                "no_reassessment": True,
                "retries": 0,
            },
        )
        save("previous_ledger", previous)
        save("approved_request", request.model_dump(mode="json"))
        verify_live_state()
        updated = copy.deepcopy(previous)
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
            deployment_revision=previous["deployment_revision"],
        )
        aggregate = ChallengeLedger(adapter, counter)

        class OneChallenge:
            deployment_revision = aggregate.deployment_revision

            async def assess(self, *args, **kwargs):
                raise DispatchDenied("AUTHORIZATION_DENIED")

            async def challenge(self, req, **kwargs):
                if (
                    invoked
                    or req != request
                    or digest(req.model_dump_json()) != REQUEST_HASH
                    or provider_request_json(req, "CHALLENGE") != payload
                    or kwargs["max_output_tokens"] != 5000
                    or budget.count(req, "CHALLENGE") + 5000 != reserve
                ):
                    save(
                        "dispatch_denied",
                        {"reason": "AUTHORIZATION_DENIED", "dispatched": False},
                    )
                    raise DispatchDenied("AUTHORIZATION_DENIED")
                verify_live_state()
                invoked.append("CHALLENGE")
                save(
                    "challenge_dispatch",
                    {
                        "input_estimate": 16019,
                        "reservation": reserve,
                        "max_output_tokens": 5000,
                    },
                )
                return await aggregate.challenge(req, **kwargs)

        evaluation_start = time.monotonic()
        result = await DecisionIntegrityEvaluator(
            OneChallenge(),
            source=source,
            authority=authority,
            settings=settings,
            token_counter=counter,
        ).evaluate(ctx, preserved_assessment=preserved)
        elapsed = time.monotonic() - evaluation_start
        save("result", result.model_dump(mode="json"))
        assert EvaluationResult.model_validate_json(result.model_dump_json()) == result
        # Raw usage may survive an SDK parsing failure. Reconcile only call_09.
        ledger = read("aggregate_usage.json")
        assert ledger["calls"][:8] == previous["calls"] and len(
            ledger["calls"]
        ) == 8 + len(invoked)
        if invoked and (ROOT / "call_09.provider.json").exists():
            provider = read("call_09.provider.json")
            usage = provider.get("usage")
            entry = ledger["calls"][8]
            if usage and entry.get("input_tokens") is None:
                entry.update(
                    input_tokens=usage["prompt_tokens"],
                    output_tokens=usage["completion_tokens"],
                    charged_tokens=usage["total_tokens"],
                    request_id=provider.get("id"),
                )
                temporary.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
                temporary.replace(ROOT / "aggregate_usage.json")
        verify_live_state()
        assert {f.finding_id: f.candidate for f in result.findings} == {
            f.finding_id: f.candidate for f in historical_result.findings
        }
        save(
            "integrity",
            {
                "historical_files_unchanged": True,
                "historical_file_count": len(protected) - 1,
                "historical_entries_unchanged": True,
                "production_sources_unchanged_during_run": True,
                "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
            },
        )
        summary = {
            "outcome": result.outcome,
            "execution": result.execution,
            "coverage": result.coverage.model_dump(mode="json"),
            "http_phases": phases,
            "evaluator_call_records": [c.model_dump(mode="json") for c in result.calls],
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "kind": f.kind,
                    "disposition": f.disposition,
                    "attribution": f.attribution,
                    "review_error": f.review_error,
                    "challenge_state": f.challenge_state,
                    "review": f.review.model_dump(mode="json") if f.review else None,
                    "review_rejection": f.review_rejection.model_dump(mode="json")
                    if f.review_rejection
                    else None,
                }
                for f in result.findings
            ],
            "new_ledger_entries": ledger["calls"][8:],
            "aggregate_charged_tokens": sum(
                c["charged_tokens"] for c in ledger["calls"]
            ),
            "remaining_tokens": CEILING
            - sum(c["charged_tokens"] for c in ledger["calls"]),
            "evaluation_latency_seconds": elapsed,
            "runner_latency_seconds": time.monotonic() - started,
            "historical_assessment_reused": True,
            "assessment_api_calls": 0,
            "challenge_api_calls": len(phases),
        }
        save("summary", summary)
        print(json.dumps(summary, indent=2))
    except BaseException as error:
        if not (ROOT / f"A_{TAG}_failure.json").exists():
            save(
                "failure",
                {
                    "error_type": type(error).__name__,
                    "repeat_forbidden": True,
                    "elapsed_seconds": time.monotonic() - started,
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
        raise SystemExit("Requires this challenge-only authorization; never repeat")
    asyncio.run(run())
