"""Generic revision-owned selection through the actual wire adapter and engine."""

import copy
import json

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    ChallengeBatchOutput,
    EvaluationResult,
    EvaluationSettings,
)
from ai_services.decision_guard.evaluation.validation import validate_issue
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
    expand_source_texts,
    provider_request_json,
    source_references,
)
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    evaluate,
    fixture,
)
from ai_services.tests.test_response_contract_robustness import OfflineClient, adapter


class WireModel(ScriptedModel):
    def __init__(self, edit=None, challenge_edit=None):
        super().__init__({("ADR", "service.py"): "conflict"})
        self.edit, self.challenge_edit = edit, challenge_edit

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)
        wire = compact(
            AssessmentBatchOutput.model_validate_json(reply.output_json), request
        )
        data = wire.model_dump(mode="json")
        if self.edit:
            self.edit(data, request)
        wire = WireAssessment.model_validate_json(json.dumps(data))
        return await adapter(OfflineClient(wire)).assess(request, **kwargs)

    async def challenge(self, request, **kwargs):
        reply = await super().challenge(request, **kwargs)
        wire = compact(
            ChallengeBatchOutput.model_validate_json(reply.output_json), request
        )
        data = wire.model_dump(mode="json")
        if self.challenge_edit:
            self.challenge_edit(data, request)
        wire = WireChallenge.model_validate_json(json.dumps(data))
        return await adapter(OfflineClient(wire)).challenge(request, **kwargs)


async def prepared(**kwargs):
    ctx, source = await fixture(**kwargs)
    model = WireModel()
    result = await evaluate(ctx, source, model)
    return ctx, source, model, result


async def test_real_adapter_candidate_reaches_independent_challenge_and_final_audit():
    _, _, model, result = await prepared()
    assert result.outcome == "CONFLICTS_FOUND"
    assert len(model.requests) == len(model.challenge_requests) == 1
    assert result.findings[0].review.witness_correspondence == "UNCHANGED"
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert len(result.calls) == 2


async def test_equal_text_and_equal_commit_keep_three_distinct_source_owners():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    # Both baseline roles share a commit; each also shares unchanged function text with head.
    text = "def handler():"
    refs = source_references(req)
    choices = {
        role: next(
            (r, e)
            for r, e in refs.items()
            if e.kind == "CODE" and e.origin == role and text in e.excerpt
        )
        for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD")
    }
    assert choices["MERGE_BASE"][1].commit_sha == choices["TARGET_BASE"][1].commit_sha
    assert len({r for r, _ in choices.values()}) == 3
    assert len({e.evidence_id for _, e in choices.values()}) == 3
    for role, (ref, e) in choices.items():
        assert ref.startswith(role + ":") and e.origin == role


async def test_projection_preserves_identity_and_shares_only_bytes():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    before = req.model_dump_json()
    wire = json.loads(provider_request_json(req, "ASSESS"))
    assert wire["input_format"] == "dg-request-text-v2"
    original = {e.evidence_id: e for e in req.evidence}
    for e in wire["evidence"]:
        if e["kind"] == "CODE" and not e.get("read_only"):
            assert e["source_ref"].startswith(e["origin"] + ":")
        assert e["evidence_id"] in original
    restored = expand_source_texts(wire)
    assert restored["evidence"] == [e.model_dump(mode="json") for e in req.evidence]
    assert len(wire["source_texts"]) < len(wire["evidence"])
    assert req.model_dump_json() == before


async def test_read_only_indexed_code_has_no_selectable_revision_alias():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    original = next(e for e in req.evidence if e.kind == "CODE")
    indexed = original.model_copy(
        update={"evidence_id": "indexed-only", "origin": "INDEXED_CODE"}
    )
    req = req.model_copy(update={"evidence": (*req.evidence, indexed)})
    wire = json.loads(provider_request_json(req, "ASSESS"))
    row = next(e for e in wire["evidence"] if e["evidence_id"] == "indexed-only")
    assert row["read_only"] and "source_ref" not in row
    assert "indexed-only" not in {
        e.evidence_id for e in source_references(req).values()
    }


async def test_source_aliases_are_deterministic_under_input_permutation():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    reordered = req.model_copy(update={"evidence": tuple(reversed(req.evidence))})
    assert source_references(req) == source_references(reordered)


async def test_existing_source_outside_unit_cannot_become_its_support():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    output = AssessmentBatchOutput.model_validate_json(
        (await model.assess(req, max_output_tokens=5000, timeout=1)).output_json
    )
    proposal = next(u for u in output.units if u.issues)
    unit = next(u for u in req.units if u.unit_id == proposal.unit_id)
    original = next(
        e for e in req.evidence if e.origin == "PR_HEAD" and e.kind == "CODE"
    )
    foreign = original.model_copy(
        update={
            "evidence_id": "other-unit-source",
            "path": "other.py",
            "location": None,
        }
    )
    req = req.model_copy(update={"evidence": (*req.evidence, foreign)})
    data = compact(output, req).model_dump(mode="json")
    source_ref = next(
        ref
        for ref, e in source_references(req).items()
        if e.evidence_id == foreign.evidence_id
    )
    data["quotes"].append(
        {"quote_id": "H9999", "source_ref": source_ref, "quote": foreign.excerpt[:20]}
    )
    next(u["issues"][0] for u in data["units"] if u["issues"])["core"]["citations"] = [
        "H9999"
    ]
    expanded = WireAssessment.model_validate_json(json.dumps(data)).expanded(req)
    issue = next(u.issues[0] for u in expanded.units if u.issues)
    with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE"):
        validate_issue(proposal, issue, req, unit, "foreign-source")


@pytest.mark.parametrize("fault", ["duplicate", "foreign_owner", "wrong_commit"])
async def test_bad_original_identity_is_not_given_trusted_alias(fault):
    _, _, model, _ = await prepared()
    req = model.requests[0]
    e = next(e for e in req.evidence if e.kind == "CODE" and e.origin == "PR_HEAD")
    if fault == "duplicate":
        evidence = (*req.evidence, e)
    else:
        bad = e.model_copy(
            update={"repository_id": "another-repository"}
            if fault == "foreign_owner"
            else {"commit_sha": "f" * 40}
        )
        evidence = tuple(bad if row == e else row for row in req.evidence)
    with pytest.raises(
        ValueError,
        match="AMBIGUOUS_SOURCE_IDENTITY|INVALID_EVIDENCE_IDENTITY|WRONG_SOURCE_REVISION",
    ):
        source_references(req.model_copy(update={"evidence": evidence}))


@pytest.mark.parametrize("field", ["core", "MERGE_BASE", "TARGET_BASE", "PR_HEAD"])
async def test_wrong_revision_quote_reuse_rejects_only_its_finding(field):
    ctx, source = await fixture()

    def edit(data, req):
        unit = next(u for u in data["units"] if u["issues"])
        good = unit["issues"][0]
        bad = copy.deepcopy(good)
        bad["claim"] += " independent invalid citation"
        wrong = next(
            r
            for r in good["revisions"]
            if r["revision"]
            == ("MERGE_BASE" if field in {"core", "PR_HEAD"} else "PR_HEAD")
        )
        target = (
            bad["core"]
            if field == "core"
            else next(r for r in bad["revisions"] if r["revision"] == field)
        )
        target["citations"] = wrong["citations"]
        unit["issues"].append(bad)

    model = WireModel(edit)
    result = await evaluate(ctx, source, model)
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    assert result.rejected_findings[0].reason == "WRONG_REVISION_CITATION"
    assert result.coverage.uncertain == 1 and result.coverage.scope == "PARTIAL"
    assert len(model.challenge_requests[0].candidates) == 1


@pytest.mark.parametrize(
    "fault", ["prefix", "unknown_source", "adr_as_code", "unknown_quote"]
)
async def test_invalid_reference_does_not_become_valid_through_expansion(fault):
    ctx, source = await fixture()

    def edit(data, req):
        issue = next(u["issues"][0] for u in data["units"] if u["issues"])
        original = next(
            q for q in data["quotes"] if q["quote_id"] == issue["core"]["citations"][0]
        )
        if fault == "adr_as_code":
            issue["core"]["citations"] = issue["rule"]["citations"]
        elif fault == "unknown_quote":
            issue["core"]["citations"] = ["H9999"]
        else:
            extra = {**original, "quote_id": "H9998", "quote": original["quote"][:10]}
            extra["source_ref"] = (
                "MERGE_BASE:1" if fault == "prefix" else "PR_HEAD:99999"
            )
            data["quotes"].append(extra)
            issue["core"]["citations"] = ["H9998"]

    result = await evaluate(
        ctx, source, WireModel(edit), settings=EvaluationSettings(retries=0)
    )
    assert result.outcome == "REVIEW_REQUIRED" and result.coverage.uncertain == 1
    assert result.rejected_findings
    assert not result.findings


async def test_quote_duplicates_rejected_but_same_text_across_owners_is_not_merged():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    output = AssessmentBatchOutput.model_validate_json(
        (await model.assess(req, max_output_tokens=5000, timeout=1)).output_json
    )
    data = compact(output, req).model_dump(mode="json")
    data["quotes"].append({**data["quotes"][0], "quote_id": "A9999"})
    with pytest.raises(ValueError, match="DUPLICATE_QUOTE_DEFINITION"):
        WireAssessment.model_validate_json(json.dumps(data)).expanded(req)
    # Baseline code quotes have the same bytes in this repository.
    refs = compact(output, req).quotes
    assert any(
        a.quote == b.quote and a.source_ref != b.source_ref for a in refs for b in refs
    )


async def test_diverged_target_ownership_remains_independent_of_the_head_change():
    ctx, source = await fixture(
        merge="c" * 40,
        target="def handler():\n    orderTotal = 3\n    return orderTotal\n",
    )
    model = WireModel()
    result = await evaluate(ctx, source, model)
    assert ctx.revision.merge_base_sha != ctx.revision.base_sha
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    req = model.requests[0]
    for ref, e in source_references(req).items():
        if ref.startswith("TARGET_BASE:"):
            assert e.commit_sha == ctx.revision.base_sha
        if ref.startswith("MERGE_BASE:"):
            assert e.commit_sha == ctx.revision.merge_base_sha


@pytest.mark.parametrize("fault", ["revision", "witness"])
async def test_challenger_cannot_confirm_wrong_revision_or_uncertain_witness(fault):
    ctx, source = await fixture()

    def edit(data, req):
        d = data["decisions"][0]
        if fault == "witness":
            d["witness_correspondence"] = "UNCERTAIN"
        else:
            next(r for r in d["revisions"] if r["revision"] == "PR_HEAD")[
                "citations"
            ] = next(r for r in d["revisions"] if r["revision"] == "MERGE_BASE")[
                "citations"
            ]

    result = await evaluate(ctx, source, WireModel(challenge_edit=edit))
    assert result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.findings[0].review_error
    data = json.loads(result.model_dump_json())
    data["findings"][0]["disposition"] = "SUPPORTED_CONFLICT"
    data["outcome"] = "CONFLICTS_FOUND"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate_json(json.dumps(data))


async def test_projection_reference_tampering_fails_identity_roundtrip():
    _, _, model, _ = await prepared()
    data = json.loads(provider_request_json(model.requests[0], "ASSESS"))
    row = next(
        e for e in data["evidence"] if e.get("source_ref", "").startswith("PR_HEAD:")
    )
    row["source_ref"] = "TARGET_BASE:1"
    with pytest.raises(ValueError, match="ownership mismatch"):
        expand_source_texts(data)


async def test_entire_identical_source_still_has_three_distinct_revision_handles():
    _, _, model, _ = await prepared()
    req = model.requests[0]
    original = next(
        e for e in req.evidence if e.kind == "CODE" and e.origin == "PR_HEAD"
    )
    sources = tuple(
        original.model_copy(
            update={
                "evidence_id": "same-text:" + role,
                "origin": role,
                "commit_sha": req.revision.head_sha
                if role == "PR_HEAD"
                else req.revision.base_sha,
            }
        )
        for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD")
    )
    refs = source_references(req.model_copy(update={"evidence": sources}))
    assert len(refs) == 3 and len({e.excerpt for e in refs.values()}) == 1
    assert {ref.split(":")[0] for ref in refs} == {e.origin for e in sources}


@pytest.mark.parametrize("fault", ["missing_source", "audit_id", "candidate_quote"])
async def test_wire_requires_explicit_source_ref_and_independent_quote_namespace(fault):
    _, _, model, _ = await prepared()
    req = model.requests[0]
    output = AssessmentBatchOutput.model_validate_json(
        (await model.assess(req, max_output_tokens=5000, timeout=1)).output_json
    )
    data = compact(output, req).model_dump(mode="json")
    q = data["quotes"][0]
    if fault == "missing_source":
        q.pop("source_ref")
    elif fault == "audit_id":
        q["source_ref"] = next(iter(source_references(req).values())).evidence_id
    else:
        q["quote_id"] = "c" + q["quote_id"]
    with pytest.raises(ValidationError):
        WireAssessment.model_validate_json(json.dumps(data))


def test_exact_call_05_is_legacy_and_its_original_revision_errors_remain_rejected():
    from ai_services.decision_guard.evaluation.models import AssessmentRequest
    from ai_services.runs.prepare_stage_4_2_live_validation import ROOT

    path = ROOT / "call_05.provider.json"
    if not path.exists():
        pytest.skip("Private preserved live artifact unavailable")
    original = path.read_bytes()
    wire = json.loads(json.loads(original)["choices"][0]["message"]["content"])
    with pytest.raises(ValidationError):
        WireAssessment.model_validate_json(json.dumps(wire))
    # Lossless legacy quote expansion only. No role substitution or regeneration.
    definitions = {
        q["quote_id"]: {k: v for k, v in q.items() if k != "quote_id"}
        for q in wire.pop("quotes")
    }

    def expand_legacy(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"citations", "support"}:
                    value[key] = [definitions[q] for q in item]
                else:
                    expand_legacy(item)
        elif isinstance(value, list):
            for item in value:
                expand_legacy(item)

    expand_legacy(wire)
    output = AssessmentBatchOutput.model_validate_json(json.dumps(wire))
    reply = json.loads((ROOT / "call_05.reply.json").read_text())
    assert output == AssessmentBatchOutput.model_validate_json(reply["output_json"])
    req = AssessmentRequest.model_validate_json(
        (ROOT / "call_05.request.json").read_text()
    )
    proposal = next(u for u in output.units if u.issues)
    unit = next(u for u in req.units if u.unit_id == proposal.unit_id)
    with pytest.raises(ValueError, match="WRONG_REVISION_CITATION"):
        validate_issue(proposal, proposal.issues[0], req, unit, "audit")
    assert path.read_bytes() == original
