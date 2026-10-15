"""Final v6 offline-only context reconstruction, exact sizing and preserved replay."""

import ast
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import tiktoken
from dotenv import load_dotenv
from openai.lib._parsing import parse_chat_completion
from openai.types.chat import ChatCompletion

from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    RequestBudget,
)
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
    PROMPT_VERSION,
    wire_text,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AssessmentRequest,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_proposal,
    validate_support,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    expand_source_texts,
    provider_request_json,
)
from ai_services.decision_guard.hybrid import HybridContextRetrieval, RetrievalSettings
from ai_services.decision_guard.hybrid.models import SearchIndex
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.runs.benchmark_stage_4_1_hybrid_context import ArchiveContextGraph
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureReadiness,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

TAG = "v6_final"


def read(n):
    return json.loads((ROOT / n).read_text(encoding="utf-8"))


def out(n):
    return ROOT / f"A_{TAG}_{n}.json"


async def main():
    if list(ROOT.glob(f"A_{TAG}_*.json")):
        raise FileExistsError("Preserve existing validation tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    production = {
        str(p): file_hash(p) for p in Path("ai_services/decision_guard").rglob("*.py")
    }
    assert PROMPT_VERSION == "dg-semantic-v6"
    ast.parse(
        Path("ai_services/decision_guard/evaluation/model_adapter.py").read_text(
            encoding="utf-8"
        )
    )
    manifest = read("manifest.json")
    archive_text = (ROOT / "claimiq_graph_snapshot.json").read_text(encoding="utf-8")
    assert digest(archive_text) == manifest["snapshot_hash"]
    event = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    index = SearchIndex.model_validate_json(
        (ROOT / "A_step2b_chunk_reuse_index.json").read_text(encoding="utf-8")
    )
    source, _ = source_for(
        event.repository_id,
        Path(next(c for c in manifest["cases"] if c["id"] == "A")["source_root"]),
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(enc.encode(t, disallowed_special=())),
    )
    settings = EvaluationSettings.model_validate(manifest["settings"])
    budget = RequestBudget(settings, counter)
    old_packets = [
        AssessmentRequest.model_validate_json(json.dumps(p))
        for p in read("A_semantic_live_v1_without_semantic_packets.json")
    ]
    old_sizes = [budget.count(p, "ASSESS") for p in old_packets]
    sizing = ContextRequestBudget(
        source, authority, settings=settings, token_counter=counter
    )
    builder = ArchitectureContextBuilder(
        ArchiveContextGraph(json.loads(archive_text)),
        source,
        FixtureReadiness(
            event.repository_id,
            event.base_sha,
            manifest["snapshot_hash"],
            event.github_repository_id,
            event.base_branch,
        ),
        lambda _: asyncio.Lock(),
        settings=ContextSettings(max_decisions=4, max_constraints=8),
        token_counter=counter,
        request_budget=sizing,
        hybrid=HybridContextRetrieval(
            index=index,
            authority=authority,
            mode="ARCHIVED",
            settings=RetrievalSettings(semantic=False),
        ),
    )
    ctx = validate_context(await builder.build(event))
    packets = sizing.last_packets
    old_ctx = ArchitectureContext.model_validate_json(
        (ROOT / "A_semantic_live_v1_without_semantic_context.json").read_text(
            encoding="utf-8"
        )
    )
    assert ctx.revision == old_ctx.revision and ctx.adr_snapshot == old_ctx.adr_snapshot
    assert all(a.status == "APPROVED" for a in authority.receipts(ctx))
    # Only after fresh discovery/selection/packing: label for test targeting.
    expected = (
        ArchitectureContext.model_validate_json(
            (ROOT / "A_context.json").read_text(encoding="utf-8")
        )
        .requirements[0]
        .requirement_id
    )
    matches = [
        (i, p)
        for i, p in enumerate(packets, 1)
        if any(u.rule_id == expected for u in p.units)
    ]
    if len(matches) > 1:
        raise ValueError("Ambiguous target packets")
    ordinal, selected = matches[0] if matches else (None, None)
    metric = []
    for number, p in enumerate(packets, 1):
        wire = provider_request_json(p, "ASSESS")
        expanded = expand_source_texts(json.loads(wire))
        assert {e["evidence_id"]: e["excerpt"] for e in expanded["evidence"]} == {
            e.evidence_id: e.excerpt for e in p.evidence
        }
        assert budget.fits(p, "ASSESS")
        assert all(e.repository_id == ctx.revision.repository_id for e in p.evidence)
        assert all(e.excerpt_hash == digest(e.excerpt) for e in p.evidence)
        for o in p.observations:
            if o.status == "PRESENT":
                e = next(e for e in p.evidence if e.evidence_id == o.evidence_id)
                assert e.origin == o.revision and e.commit_sha == getattr(
                    ctx.revision,
                    {
                        "MERGE_BASE": "merge_base_sha",
                        "TARGET_BASE": "base_sha",
                        "PR_HEAD": "head_sha",
                    }[o.revision],
                )
        metric.append(
            {
                "ordinal": number,
                "batch_id": p.batch_id,
                "unit_ids": [u.unit_id for u in p.units],
                "rules": [u.rule_id for u in p.units],
                "input_tokens": budget.count(p, "ASSESS"),
                "output_reserve": settings.max_output_tokens,
                "reservation": budget.count(p, "ASSESS") + settings.max_output_tokens,
                "fits": True,
                "canonical_sha256": digest(p.model_dump_json()),
                "provider_payload_sha256": digest(wire),
                "wire_text_sha256": digest(wire_text(p, "ASSESS")),
                "evidence_ids": [e.evidence_id for e in p.evidence],
                "source_observations": [
                    o.model_dump(mode="json") for o in p.observations
                ],
                "policy_observations": [
                    o.model_dump(mode="json") for o in p.policy_observations
                ],
                "repository_revision_hashes_valid": True,
                "provider_text_lossless": True,
            }
        )
    # Raw response replay, with no constructed Azure client or transport.
    original = AssessmentRequest.model_validate_json(
        (ROOT / "A_controlled_preflight_v1_request.json").read_text(encoding="utf-8")
    )
    raw = (ROOT / "call_03.provider.json").read_bytes()

    class Local:
        def __init__(self):
            self.beta = self.chat = self.completions = self

        def with_options(self, **kw):
            return self

        async def parse(self, **kw):
            return parse_chat_completion(
                response_format=kw["response_format"],
                input_tools=[],
                chat_completion=ChatCompletion.model_validate_json(raw),
            )

    reply = await AzureEvaluationModel(
        SimpleNamespace(client=Local(), deployment="offline"),
        deployment_revision="offline",
    ).assess(original, max_output_tokens=5000, timeout=90)
    assert reply.status == "OK"
    output = AssessmentBatchOutput.model_validate_json(reply.output_json)
    try:
        validate_proposal(output.units[0], original, original.units[0])
        source_error = None
    except ValueError as e:
        source_error = str(e)
    roles = []
    for r in output.units[0].issues[0].revisions:
        try:
            validate_support(
                r.citations,
                r.observation_ids,
                original,
                original.units[0],
                role=r.revision,
            )
        except ValueError as e:
            roles.append({"revision": r.revision, "error": str(e)})
    assert source_error == "QUOTE_NOT_FOUND" and any(
        r["error"] == "WRONG_REVISION_CITATION" for r in roles
    )
    ledger = read("aggregate_usage.json")
    charged = sum(c["charged_tokens"] for c in ledger["calls"])
    assert len(ledger["calls"]) == 3 and charged == 53227
    remaining = ledger["max_tokens"] - charged
    reservation = (
        budget.count(selected, "ASSESS") + settings.max_output_tokens
        if selected
        else None
    )
    load_dotenv(".env")
    configuration = {
        "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"),
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION"),
        "endpoint_configured": bool(os.getenv("AZURE_OPENAI_ENDPOINT")),
        "credentials_configured": bool(os.getenv("AZURE_OPENAI_API_KEY")),
    }
    assert (
        configuration["deployment"] == manifest["deployment"]
        and configuration["api_version"] == manifest["api_version"]
        and all(configuration.values())
    )
    result = {
        "status": "OFFLINE_VALIDATION_COMPLETE_NOT_AUTHORIZED",
        "prompt_version": PROMPT_VERSION,
        "assessment_prompt_sha256": digest(ASSESS_PROMPT),
        "challenge_prompt_sha256": digest(CHALLENGE_PROMPT),
        "assessment_prompt_tokens": counter(ASSESS_PROMPT),
        "challenge_prompt_tokens": counter(CHALLENGE_PROMPT),
        "wire_assessment_schema_sha256": digest(
            json.dumps(WireAssessment.model_json_schema(), sort_keys=True)
        ),
        "wire_challenge_schema_sha256": digest(
            json.dumps(WireChallenge.model_json_schema(), sort_keys=True)
        ),
        "configuration": configuration,
        "settings": settings.model_dump(mode="json"),
        "context_hash": ctx.context_hash,
        "revision": ctx.revision.model_dump(mode="json"),
        "retained_requirements": [r.requirement_id for r in ctx.requirements],
        "governing_requirement_retained": bool(matches),
        "governing_requirement_id": expected,
        "all_known_unit_count": sum(len(p.units) for p in packets),
        "context_limitations": [
            l.model_dump(mode="json") for l in ctx.coverage.limitations
        ],
        "context_tokens": counter(model_context_json(ctx)),
        "context_entities": len(ctx.entities),
        "context_relationships": len(ctx.relationships),
        "old_packet_inputs_with_v6": old_sizes,
        "packets": metric,
        "selected_packet_ordinal": ordinal,
        "selected_batch_id": selected.batch_id if selected else None,
        "selected_unit_ids": [u.unit_id for u in selected.units] if selected else [],
        "selected_input_tokens": budget.count(selected, "ASSESS") if selected else None,
        "selected_reservation": reservation,
        "ledger_charged_tokens": charged,
        "ledger_remaining_tokens": remaining,
        "minimum_token_ceiling_increase": max(0, reservation - remaining)
        if reservation
        else None,
        "minimum_new_aggregate_token_ceiling": charged + reservation
        if reservation
        else None,
        "replay": {
            "adapter_status": reply.status,
            "source_error": source_error,
            "role_errors": roles,
            "unused_diagnostics": [
                d.model_dump(mode="json") for d in reply.response_diagnostics
            ],
            "eligible": False,
        },
        "billable_calls": 0,
        "fixture_readiness_only": True,
        "aggregate_reservation": sum(m["reservation"] for m in metric),
    }
    assert all(file_hash(ROOT / n) == h for n, h in protected.items()) and all(
        file_hash(Path(n)) == h for n, h in production.items()
    )
    save(out("preflight"), result)
    save(out("context"), ctx.model_dump(mode="json"))
    save(out("packets"), [p.model_dump(mode="json") for p in packets])
    if selected:
        save(out("selected_request"), selected.model_dump(mode="json"))
        save(
            out("selected_provider_payload"),
            json.loads(provider_request_json(selected, "ASSESS")),
        )
    save(out("replayed_reply"), reply.model_dump(mode="json"))
    save(out("plan"), builder.last_retrieval_plan.model_dump(mode="json"))
    save(
        out("integrity"),
        {
            "historical_sha256": protected,
            "production_sha256": production,
            "unchanged": True,
            "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
            "billable_calls": 0,
        },
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "old_packet_inputs_with_v6",
                    "context_tokens",
                    "packets",
                    "selected_packet_ordinal",
                    "selected_input_tokens",
                    "selected_reservation",
                    "minimum_token_ceiling_increase",
                    "minimum_new_aggregate_token_ceiling",
                ]
            },
            indent=2,
        )[:2200]
    )


if __name__ == "__main__":
    asyncio.run(main())
