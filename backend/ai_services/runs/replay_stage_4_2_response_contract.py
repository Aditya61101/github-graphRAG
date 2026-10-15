"""Replay preserved bytes through the production adapter with a local SDK parser."""

import asyncio
import json
from types import SimpleNamespace

import tiktoken
from openai.lib._parsing import parse_chat_completion
from openai.types.chat import ChatCompletion

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import AzureEvaluationModel
from ai_services.decision_guard.evaluation.engine import RequestBudget
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
    EvaluationSettings,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_issue,
    validate_proposal,
    validate_support,
)
from ai_services.decision_guard.evaluation.wire import WireAssessment
from ai_services.decision_guard.models import stable_id
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT

TAG = "response_contract_v1"


async def replay():
    if list(ROOT.glob(f"A_{TAG}_*.json")):
        raise FileExistsError("Preserve prior replay")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    body = (ROOT / "call_03.provider.json").read_text(encoding="utf-8")
    request = AssessmentRequest.model_validate_json(
        (ROOT / "A_controlled_preflight_v1_request.json").read_text(encoding="utf-8")
    )
    old = json.loads((ROOT / "call_03.reply.json").read_text(encoding="utf-8"))

    class LocalClient:
        def __init__(self):
            self.beta = self.chat = self.completions = self
            self.calls = 0

        def with_options(self, **kw):
            assert kw["max_retries"] == 0
            return self

        async def parse(self, **kw):
            self.calls += 1
            assert kw["response_format"] is WireAssessment
            return parse_chat_completion(
                response_format=kw["response_format"],
                input_tools=[],
                chat_completion=ChatCompletion.model_validate_json(body),
            )

    local = LocalClient()
    model = AzureEvaluationModel(
        SimpleNamespace(client=local, deployment="aicloud-gpt-5.4"),
        deployment_revision="aicloud-gpt-5.4:configured:2024-12-01-preview",
    )
    reply = await model.assess(request, max_output_tokens=5000, timeout=90)
    assert local.calls == 1 and reply.status == "OK"
    output = AssessmentBatchOutput.model_validate_json(reply.output_json)
    assert (
        output.batch_id == request.batch_id
        and len(output.units) == len(request.units)
        and {u.unit_id for u in output.units} == {u.unit_id for u in request.units}
    )
    evidence = {e.evidence_id: e for e in request.evidence}
    refs = {}

    def walk(value):
        if isinstance(value, dict):
            if "evidence_id" in value and "quote" in value:
                refs[value["evidence_id"], value["quote"]] = value
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(output.model_dump(mode="json"))
    quotes = []
    for eid, quote in refs:
        row = {"evidence_id": eid, "quote": quote}
        try:
            row.update(
                valid=True,
                citation=resolve_quote(
                    QuoteRef(evidence_id=eid, quote=quote), evidence
                ).model_dump(mode="json"),
            )
        except ValueError as e:
            row.update(valid=False, reason=str(e))
        quotes.append(row)
    units = []
    role_errors = []
    for proposal in output.units:
        unit = next(u for u in request.units if u.unit_id == proposal.unit_id)
        row = {"unit_id": unit.unit_id, "eligible": False}
        try:
            validate_proposal(proposal, request, unit)
            row["proposal_valid"] = True
            for index, issue in enumerate(proposal.issues):
                _, _, _, eligible = validate_issue(
                    proposal,
                    issue,
                    request,
                    unit,
                    stable_id(unit.unit_id, str(index), issue.claim),
                )
                row["eligible"] |= eligible
        except ValueError as e:
            row.update(
                proposal_valid=False, error=str(e), production_unit_status="UNCERTAIN"
            )
        for issue in proposal.issues:
            for rev in issue.revisions:
                try:
                    validate_support(
                        rev.citations,
                        rev.observation_ids,
                        request,
                        unit,
                        role=rev.revision,
                    )
                except ValueError as e:
                    role_errors.append(
                        {
                            "field": "RevisionAssessment",
                            "revision": rev.revision,
                            "error": str(e),
                        }
                    )
            for claim in issue.claims:
                role = {
                    "HEAD_BEHAVIOR": "PR_HEAD",
                    "BASE_BEHAVIOR": "MERGE_BASE",
                    "TARGET_COMPATIBILITY": "TARGET_BASE",
                }.get(claim.obligation)
                if role:
                    try:
                        validate_support(
                            claim.citations,
                            claim.observation_ids,
                            request,
                            unit,
                            role=role,
                        )
                    except ValueError as e:
                        role_errors.append(
                            {
                                "field": claim.obligation,
                                "revision": role,
                                "error": str(e),
                            }
                        )
        units.append(row)
    pre = json.loads(
        (ROOT / "A_controlled_preflight_v1_preflight.json").read_text(encoding="utf-8")
    )
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda s: len(enc.encode(s, disallowed_special=())),
    )
    count = RequestBudget(
        EvaluationSettings.model_validate(pre["settings"]), counter
    ).count(request, "ASSESS")
    assert count == pre["selected_input_tokens"] == 18854
    assert not any(u["eligible"] for u in units)
    result = {
        "offline": True,
        "billable_calls": 0,
        "original_adapter_status": old["status"],
        "new_adapter_status": reply.status,
        "response_diagnostics": [
            d.model_dump(mode="json") for d in reply.response_diagnostics
        ],
        "quote_validation": quotes,
        "role_validation": role_errors,
        "unit_validation": units,
        "original_request_input_tokens": pre["selected_input_tokens"],
        "refined_request_input_tokens": count,
        "token_delta": count - pre["selected_input_tokens"],
        "provider_body_sha256": protected["call_03.provider.json"],
        "challenge_executed": False,
        "confirmed_findings": 0,
        "classification": "ADAPTER_OK_BUT_PROPOSAL_SOURCE_VALIDATION_FAILED",
        "historical_artifacts_unchanged": True,
    }
    assert all(file_hash(ROOT / n) == h for n, h in protected.items())
    save(ROOT / f"A_{TAG}_replayed_reply.json", reply.model_dump(mode="json"))
    save(ROOT / f"A_{TAG}_expanded_assessment.json", output.model_dump(mode="json"))
    save(ROOT / f"A_{TAG}_replay_audit.json", result)
    save(
        ROOT / f"A_{TAG}_integrity.json",
        {
            "historical_files_unchanged": {n: True for n in protected},
            "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
            "billable_calls": 0,
        },
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "original_adapter_status",
                    "new_adapter_status",
                    "response_diagnostics",
                    "role_validation",
                    "unit_validation",
                    "token_delta",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(replay())
