"""Offline historical-response replay and exact frozen-context sizing after v5."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import tiktoken
from openai.lib._parsing import parse_chat_completion
from openai.types.chat import ChatCompletion

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    RequestBudget,
)
from ai_services.decision_guard.evaluation.evidence import (
    validate_context,
)
from ai_services.decision_guard.evaluation.model_adapter import (
    PROMPT_VERSION,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
)
from ai_services.decision_guard.evaluation.validation import (
    source_anchors,
    validate_proposal,
    validate_support,
)
from ai_services.decision_guard.models import ArchitectureContext
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

TAG = "proof_witness_v1"


async def audit():
    if (ROOT / f"A_{TAG}_audit.json").exists():
        raise FileExistsError("Preserve prior audit")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    request = AssessmentRequest.model_validate_json(
        (ROOT / "A_controlled_preflight_v1_request.json").read_text(encoding="utf-8")
    )
    body = (ROOT / "call_03.provider.json").read_bytes()

    class LocalClient:
        def __init__(self):
            self.beta = self.chat = self.completions = self
            self.calls = 0

        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0
            return self

        async def parse(self, **kwargs):
            self.calls += 1
            return parse_chat_completion(
                response_format=kwargs["response_format"],
                input_tools=[],
                chat_completion=ChatCompletion.model_validate_json(body),
            )

    client = LocalClient()
    model = AzureEvaluationModel(
        SimpleNamespace(client=client, deployment="offline-replay"),
        deployment_revision="offline",
    )
    reply = await model.assess(request, max_output_tokens=5000, timeout=90)
    assert reply.status == "OK" and client.calls == 1
    output = AssessmentBatchOutput.model_validate_json(reply.output_json)
    unit = request.units[0]
    proposal = output.units[0]
    try:
        validate_proposal(proposal, request, unit)
        failure = None
    except ValueError as error:
        failure = str(error)
    assert failure == "QUOTE_NOT_FOUND"
    role_errors = []
    for issue in proposal.issues:
        for row in issue.revisions:
            try:
                validate_support(
                    row.citations, row.observation_ids, request, unit, role=row.revision
                )
            except ValueError as error:
                role_errors.append({"revision": row.revision, "reason": str(error)})
    anchors = source_anchors(request, unit)
    facts = []
    for issue in proposal.issues:
        for fact in issue.missing_facts:
            path = anchors.get(fact.anchor_id)
            covered = any(
                o.path == path
                and o.revision == fact.revision
                and o.complete_file
                and o.status in {"PRESENT", "ABSENT"}
                for o in request.observations
            )
            facts.append(
                {
                    **fact.model_dump(mode="json"),
                    "anchor_path": path,
                    "already_complete": covered,
                    "still_core_blocking": fact.scope == "CORE",
                }
            )
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(enc.encode(t, disallowed_special=())),
    )
    settings = EvaluationSettings.model_validate(manifest["settings"])
    budget = RequestBudget(settings, counter)
    original_packets = [
        AssessmentRequest.model_validate_json(json.dumps(p))
        for p in json.loads(
            (ROOT / "A_semantic_live_v1_without_semantic_packets.json").read_text(
                encoding="utf-8"
            )
        )
    ]
    before = json.loads(
        (ROOT / "A_controlled_preflight_v1_preflight.json").read_text(encoding="utf-8")
    )["all_packet_inputs"]
    after = [budget.count(p, "ASSESS") for p in original_packets]
    assert all(budget.fits(p, "ASSESS") for p in original_packets)
    ctx = validate_context(
        ArchitectureContext.model_validate_json(
            (ROOT / "A_semantic_live_v1_without_semantic_context.json").read_text(
                encoding="utf-8"
            )
        )
    )
    source, _ = source_for(
        ctx.revision.repository_id,
        Path(next(c for c in manifest["cases"] if c["id"] == "A")["source_root"]),
    )
    sizing = ContextRequestBudget(
        source,
        UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs"),
        settings=settings,
        token_counter=counter,
    )
    await asyncio.to_thread(sizing, ctx)
    assert all(sizing.fits(p, "ASSESS") for p in sizing.last_packets)
    old_units = {u.unit_id: u for p in original_packets for u in p.units}
    new_units = {u.unit_id: u for p in sizing.last_packets for u in p.units}
    assert old_units == new_units and len(new_units) == 7
    # No original source/revision/policy data may be lost by automatic regrouping.
    for uid, original_unit in old_units.items():
        old = next(
            p for p in original_packets if any(u.unit_id == uid for u in p.units)
        )
        new = next(
            p for p in sizing.last_packets if any(u.unit_id == uid for u in p.units)
        )
        ids = set(original_unit.evidence_ids)
        assert {e.evidence_id: e for e in old.evidence if e.evidence_id in ids} == {
            e.evidence_id: e for e in new.evidence if e.evidence_id in ids
        }
    inputs = [sizing.count(p, "ASSESS") for p in sizing.last_packets]
    ledger = json.loads((ROOT / "aggregate_usage.json").read_text(encoding="utf-8"))
    assert (
        len(ledger["calls"]) == 3
        and sum(c["charged_tokens"] for c in ledger["calls"]) == 53227
    )
    result = {
        "offline": True,
        "billable_calls": 0,
        "new_prompt_version": PROMPT_VERSION,
        "original_adapter_status": "INVALID",
        "stage_4_2a_adapter_status": "OK",
        "current_adapter_status": reply.status,
        "proposal_error": failure,
        "role_errors": role_errors,
        "missing_facts_audit": facts,
        "confirmed_findings": 0,
        "challenge_executed": False,
        "original_claim": proposal.issues[0].claim,
        "original_core": proposal.issues[0].core.model_dump(mode="json"),
        "original_response_unchanged": True,
        "original_packet_inputs": before,
        "original_packets_with_v5_inputs": after,
        "input_deltas": [b - a for a, b in zip(before, after)],
        "selected_packet_input_before": before[5],
        "selected_packet_input_after": after[5],
        "selected_reservation_after": after[5] + 5000,
        "replanned_inputs": inputs,
        "replanned_packets": len(inputs),
        "replanned_units": len(new_units),
        "replanned_reservation": sum(inputs) + len(inputs) * 5000,
        "context_selection_unchanged": True,
        "unit_sources_unchanged": True,
        "replanned_units_by_packet": [
            [u.unit_id for u in p.units] for p in sizing.last_packets
        ],
        "ledger_charged_tokens": 53227,
        "ledger_remaining_tokens": 6773,
        "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
    }
    assert all(file_hash(ROOT / n) == h for n, h in protected.items())
    save(ROOT / f"A_{TAG}_replayed_reply.json", reply.model_dump(mode="json"))
    save(ROOT / f"A_{TAG}_audit.json", result)
    save(
        ROOT / f"A_{TAG}_replanned_packets.json",
        [p.model_dump(mode="json") for p in sizing.last_packets],
    )
    save(
        ROOT / f"A_{TAG}_integrity.json",
        {
            "historical_files_unchanged": {n: True for n in protected},
            "billable_calls": 0,
            "ledger_sha256": result["ledger_sha256"],
        },
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "new_prompt_version",
                    "proposal_error",
                    "original_packet_inputs",
                    "original_packets_with_v5_inputs",
                    "selected_reservation_after",
                    "replanned_inputs",
                    "replanned_packets",
                    "replanned_reservation",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(audit())
