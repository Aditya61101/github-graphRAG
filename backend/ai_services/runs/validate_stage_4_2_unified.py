"""Offline-only unified-contract audit over preserved ClaimIQ sources. No clients."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
    read_evaluation_result,
)
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget, hashed
from ai_services.decision_guard.evaluation.historical_models import (
    AssessmentBatchOutput as HistoricalAssessment,
)
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
    PROMPT_VERSION,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    CoreAllegation,
    EvidenceDiscussion,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.tests.test_decision_guard_evaluation import ScriptedModel
from pydantic import ValidationError

TAG = "A_unified_v7_revision_contract_offline"
RULE = "b9652205302af1fa53bddf46fcaa7c7fa0aecb07138504891a105f2ead97a31a"


def literal(request, role, text, *, policy=False):
    found = [
        e
        for e in request.evidence
        if e.origin == role
        and text in e.excerpt
        and (
            e.kind == "ADR"
            if policy
            else e.kind == "CODE" and e.path.endswith("claim_status.py")
        )
    ]
    if not found:
        raise ValueError("Required original fixture quotation unavailable")
    e = min(found, key=lambda e: len(e.excerpt))
    return QuoteRef(evidence_id=e.evidence_id, quote=text)


class CaseAScript(ScriptedModel):
    """Explicit test oracle; never presented as real-model reasoning."""

    async def assess(self, request, **kwargs):
        self.outcomes = {
            ("ADR", u.path): "conflict" for u in request.units if u.rule_id == RULE
        }
        reply = await super().assess(request, **kwargs)
        output = AssessmentBatchOutput.model_validate_json(reply.output_json)
        policy_text = "4. `Claim.processing_status` must only transition to `READY_FOR_EVALUATION` when every associated active `Document` has reached `status == 'VERIFIED'`."
        proposals = []
        for proposal in output.units:
            issues = []
            for issue in proposal.issues:
                quote = literal(request, "ADR_DOCUMENT", policy_text, policy=True)
                head = literal(request, "PR_HEAD", 'if "VERIFIED" in statuses:')
                revisions = tuple(
                    r.model_copy(
                        update={
                            "citations": (
                                literal(
                                    request,
                                    r.revision,
                                    'if "VERIFIED" in statuses:'
                                    if r.revision == "PR_HEAD"
                                    else 'if statuses == {"VERIFIED"}:',
                                ),
                            )
                        }
                    )
                    for r in issue.revisions
                )
                issues.append(
                    issue.model_copy(
                        update={
                            "claim": "Readiness can be returned with an active unverified document",
                            "rule": EvidenceDiscussion(
                                statement="Readiness requires every associated active document VERIFIED",
                                citations=(quote,),
                            ),
                            "core": CoreAllegation(
                                boundary="derive_processing_status readiness return",
                                witness="Two associated active documents: one VERIFIED, one UPLOADED; neither has a blocking status",
                                qualifiers=(
                                    "Both documents are active and associated with this claim",
                                ),
                                reachability="The nonempty query includes both documents, neither status is blocking, and the new membership guard returns readiness",
                                citations=(head,),
                            ),
                            "mechanism": "Membership replaces equality on the same document status set",
                            "counterevidence": EvidenceDiscussion(
                                statement="The original complete policy permits no partial-verification readiness; the complete helper's preceding failure guard does not block VERIFIED plus UPLOADED",
                                citations=(quote,),
                            ),
                            "consequence": "The local readiness return violates the qualified invariant",
                            "revisions": revisions,
                        }
                    )
                )
            proposals.append(proposal.model_copy(update={"issues": tuple(issues)}))
        self.last_output = output.model_copy(update={"units": tuple(proposals)})
        return reply.model_copy(
            update={"output_json": self.last_output.model_dump_json()}
        )

    async def challenge(self, request, **kwargs):
        # Independent fixture oracle executes only the archived pure derivation
        # function, with explicit all-active rows and a bounded query test double.
        rows = [
            SimpleNamespace(status="VERIFIED", is_active=True),
            SimpleNamespace(status="UPLOADED", is_active=True),
        ]
        assert all(row.is_active for row in rows)

        class Query:
            def query(self, _):
                return self

            def filter(self, _):
                return self

            def all(self):
                return rows

        observed = {}
        for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD"):
            observation = next(
                o
                for o in request.assessment.observations
                if o.revision == role and o.path.endswith("claim_status.py")
            )
            original = next(
                e.excerpt
                for e in request.assessment.evidence
                if e.evidence_id == observation.evidence_id
            )
            tree = ast.parse(original)
            body = [
                node
                for node in tree.body
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "derive_processing_status"
                )
                or (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "BLOCKING_STATUS"
                        for t in node.targets
                    )
                )
            ]
            assert len(body) == 2
            namespace = {
                "Claim": object,
                "Session": object,
                "Document": SimpleNamespace(claim_id=0),
            }
            exec(  # noqa: S102 -- archived pure fixture helper, no imports
                compile(
                    ast.Module(body=body, type_ignores=[]),
                    "archived-case-a-pure-helper",
                    "exec",
                ),
                namespace,
            )
            observed[role] = namespace["derive_processing_status"](
                SimpleNamespace(id=0), Query()
            )[0]
        assert observed == {
            "MERGE_BASE": "DOCUMENT_PROCESSING",
            "TARGET_BASE": "DOCUMENT_PROCESSING",
            "PR_HEAD": "READY_FOR_EVALUATION",
        }
        reply = await super().challenge(request, **kwargs)
        from ai_services.decision_guard.evaluation.models import ChallengeBatchOutput

        output = ChallengeBatchOutput.model_validate_json(reply.output_json)
        candidate = request.candidates[0]
        decisions = tuple(
            d.model_copy(
                update={
                    "citations": candidate.issue.rule.citations
                    + candidate.issue.core.citations,
                    "tested_witness": "Two associated active documents: VERIFIED and UPLOADED, neither blocking",
                    "explanation": "Original accepted rule requires all active documents VERIFIED. Independently executed the same all-active nonblocking witness against archived merge/target/head helpers: base and target remain DOCUMENT_PROCESSING, head returns readiness. Full local source and policy show no applicable safeguard for this return; no downstream outcome is assumed.",
                }
            )
            for d in output.decisions
        )
        return reply.model_copy(
            update={
                "output_json": output.model_copy(
                    update={"decisions": decisions}
                ).model_dump_json()
            }
        )


async def run():
    if list(ROOT.glob(TAG + "_*.json")):
        raise FileExistsError("Preserve prior offline audit tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    original = ArchitectureContext.model_validate_json(
        (ROOT / "A_budget_preserved_v6_final_context.json").read_text()
    )
    manifest = json.loads((ROOT / "manifest.json").read_text())
    source, _ = source_for(
        original.revision.repository_id,
        Path(next(c["source_root"] for c in manifest["cases"] if c["id"] == "A")),
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(enc.encode(t, disallowed_special=())),
    )
    settings = EvaluationSettings.model_validate(manifest["settings"])
    sizing = ContextRequestBudget(
        source, authority, settings=settings, token_counter=counter
    )
    await asyncio.to_thread(sizing, original)
    packet_metrics = [
        {
            "batch_id": p.batch_id,
            "unit_ids": [u.unit_id for u in p.units],
            "input_tokens": sizing.count(p, "ASSESS"),
            "output_reserve": settings.max_output_tokens,
            "fits": sizing.fits(p, "ASSESS"),
            "canonical_request_sha256": digest(p.model_dump_json()),
        }
        for p in sizing.last_packets
    ]
    # Separate explicitly scoped test context. No existing fixture is overwritten.
    ctx = original.model_copy(
        update={
            "requirements": tuple(
                r for r in original.requirements if r.requirement_id == RULE
            )
        }
    )
    ctx = ctx.model_copy(
        update={
            "context_hash": hashed(
                ctx.model_dump(mode="json", exclude={"context_hash", "budget"})
            )
        }
    )
    model = CaseAScript()
    result = await DecisionIntegrityEvaluator(
        model,
        source=source,
        authority=authority,
        settings=settings,
        token_counter=counter,
    ).evaluate(ctx)
    assert result.outcome == "CONFLICTS_FOUND" and len(model.challenge_requests) == 1
    assert (
        len(result.findings) == 1
        and result.findings[0].disposition == "SUPPORTED_CONFLICT"
    )
    assert result.findings[0].core.qualifiers == (
        "Both documents are active and associated with this claim",
    )
    historical = []
    for index in range(1, 5):
        path = ROOT / f"call_{index:02d}.provider.json"
        content = json.loads(path.read_text())["choices"][0]["message"]["content"]
        compatible = True
        try:
            WireAssessment.model_validate_json(content)
        except ValidationError:
            compatible = False
        old_reply = json.loads((ROOT / f"call_{index:02d}.reply.json").read_text())
        if old_reply["output_json"]:
            HistoricalAssessment.model_validate_json(old_reply["output_json"])
        historical.append(
            {
                "call": index,
                "direct_v2_replay_compatible": compatible,
                "reason": "Historical eight-claim schema lacks v2 rule/counterevidence and new witness/review contracts; no conversion performed",
            }
        )
    for filename in ("A_result.json", "A_refined_result.json"):
        old_result = read_evaluation_result((ROOT / filename).read_bytes())
        assert old_result.outcome == "REVIEW_REQUIRED"
    response = model.last_output
    assert compact(response, model.requests[0]).expanded(model.requests[0]) == response
    summary = {
        "offline_only": True,
        "billable_calls": 0,
        "prompt_version": PROMPT_VERSION,
        "prompt_hashes": {
            "assessment": digest(ASSESS_PROMPT),
            "challenge": digest(CHALLENGE_PROMPT),
        },
        "prompt_tokens": {
            "assessment": counter(ASSESS_PROMPT),
            "challenge": counter(CHALLENGE_PROMPT),
        },
        "schema_hashes": {
            "assessment": digest(
                json.dumps(WireAssessment.model_json_schema(), sort_keys=True)
            ),
            "challenge": digest(
                json.dumps(WireChallenge.model_json_schema(), sort_keys=True)
            ),
        },
        "full_preserved_context_packets": packet_metrics,
        "minimum_unit_inputs": [
            m.model_dump(mode="json") for m in sizing.last_unit_measurements
        ],
        "intrinsic_omissions": [
            m.model_dump(mode="json")
            for m in sizing.last_unit_measurements
            if m.input_tokens > settings.max_input_tokens
        ],
        "scoped_case_a": {
            "test_oracle": "scripted, not measured model accuracy",
            "original_context_hash": original.context_hash,
            "new_scoped_context_hash": ctx.context_hash,
            "revision": ctx.revision.model_dump(mode="json"),
            "outcome": result.outcome,
            "coverage": result.coverage.model_dump(mode="json"),
            "calls": [{"phase": c.phase, "status": c.status} for c in result.calls],
            "response_tokens": counter(
                compact(response, model.requests[0]).model_dump_json()
            ),
            "challenge_input_tokens": sizing.count(
                model.challenge_requests[0], "CHALLENGE"
            ),
            "attribution": result.findings[0].attribution,
        },
        "historical_replays": historical,
        "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
        "historical_json_files_unchanged": len(protected),
    }
    assert all(file_hash(ROOT / n) == h for n, h in protected.items())
    save(ROOT / (TAG + "_context.json"), ctx.model_dump(mode="json"))
    save(ROOT / (TAG + "_result.json"), result.model_dump(mode="json"))
    save(ROOT / (TAG + "_summary.json"), summary)
    save(
        ROOT / (TAG + "_integrity.json"),
        {"protected_sha256": protected, "all_unchanged": True, "billable_calls": 0},
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
