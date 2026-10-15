"""Generic reproductions of policy-role, clearance and repeated-output failures."""

import json
import zipfile
from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema

from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
)
from ai_services.decision_guard.evaluation.validation import validate_issue
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
    provider_request_json,
)
from ai_services.tests.test_decision_guard_context import changed
from ai_services.tests.test_decision_guard_evaluation import (
    REPO,
    ScriptedModel,
    TrustedAuthority,
    evaluate,
    refs,
)
from ai_services.tests.test_decision_guard_evaluation import (
    fixture as context_fixture,
)


async def fixture(**kwargs):
    kwargs.setdefault("policy", "Events must follow durable commit.")
    return await context_fixture(
        files=[
            changed(
                "service.py",
                "def handler():\n    commit()\n    publish()\n",
                "def handler():\n    publish()\n    commit()\n",
            )
        ],
        **kwargs,
    )


async def test_adr_quotes_cannot_be_revision_behavior_even_with_valid_code():
    ctx, source = await fixture(policy="Events must follow durable commit.")

    class MixedRoles(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            output = AssessmentBatchOutput.model_validate_json(reply.output_json)
            units = []
            for proposal in output.units:
                unit = next(u for u in request.units if u.unit_id == proposal.unit_id)
                policy = refs(request, unit, "ADR_DOCUMENT")
                issues = tuple(
                    issue.model_copy(
                        update={
                            "revisions": tuple(
                                r.model_copy(update={"citations": r.citations + policy})
                                for r in issue.revisions
                            )
                        }
                    )
                    for issue in proposal.issues
                )
                units.append(proposal.model_copy(update={"issues": issues}))
            return reply.model_copy(
                update={
                    "output_json": output.model_copy(
                        update={"units": tuple(units)}
                    ).model_dump_json()
                }
            )

    model = MixedRoles({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert any("WRONG_REVISION_CITATION" in u.reason_codes for u in result.units)
    assert not model.challenge_requests and not result.findings


@pytest.mark.parametrize("verdict", ["INSUFFICIENT", "REFUTED"])
async def test_source_presence_does_not_replace_independent_review(verdict):
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, challenge=verdict)
    result = await evaluate(ctx, source, model)
    assert not any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    assert len(model.challenge_requests) == 1


async def test_bounded_rule_path_clearance_still_requires_independent_challenge():
    ctx, source = await fixture(policy="Events must follow durable commit.")
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert result.outcome == "CONFLICTS_FOUND" and len(model.challenge_requests) == 1
    assert len(result.calls) == 2
    assert result.findings[0].review.verdict == "SUPPORTED"


async def test_chunk_completeness_is_not_whole_policy_clearance():
    ctx, source = await fixture(policy="Events must follow durable commit.")

    class ChunksOnly:
        receipts = TrustedAuthority().receipts

    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, authority=ChunksOnly())
    assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests
    assert not result.policy_observations


def store_policy(tmp_path, text, ctx):
    doc = ctx.adr_snapshot[0]
    path = tmp_path / REPO / doc.adr_id / "original.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    record = SimpleNamespace(
        id=doc.adr_id,
        repository_id=REPO,
        content_hash=doc.content_hash,
        status="COMPLETED",
        file_path=str(path),
        source_name="policy.md",
    )
    store = SimpleNamespace(
        get_repository=lambda _: SimpleNamespace(id=REPO, user_id="fixture-owner"),
        get_adr=lambda _: record,
    )
    return UploadAuthority(store, tmp_path), record, path


async def test_full_original_policy_exposes_exception_outside_retrieved_chunk(tmp_path):
    rule = "Events must follow durable commit."
    original = (
        rule
        + "\n\nExceptions: Non-persistent diagnostic events may be emitted before commit.\n"
    )
    ctx, source = await fixture(policy=rule, policy_document=original)
    authority, _, _ = store_policy(tmp_path, original, ctx)
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, challenge="REFUTED")
    result = await evaluate(ctx, source, model, authority=authority)
    request = model.requests[0]
    assert any(
        original.strip() == e.excerpt for e in request.evidence if e.kind == "ADR"
    )
    assert request.policy_observations[0].coverage == "TEXT_COMPLETE"
    assert (
        all(f.disposition == "REFUTED" for f in result.findings)
        and len(model.challenge_requests) == 1
    )


@pytest.mark.parametrize("invalid", ["hash", "repository", "size", "reads"])
async def test_policy_integrity_and_bounds_never_create_complete_evidence(
    tmp_path, invalid
):
    policy = "Events must follow durable commit."
    ctx, _ = await fixture(policy=policy)
    authority, record, path = store_policy(tmp_path, policy, ctx)
    settings = EvaluationSettings()
    if invalid == "hash":
        path.write_bytes(b"changed")
    if invalid == "repository":
        record.repository_id = "foreign"
    if invalid == "size":
        settings = settings.model_copy(update={"max_policy_read_bytes": 1})
    if invalid == "reads":
        settings = settings.model_copy(update={"max_policy_reads": 0})
    bundle = authority.policy_evidence(
        ctx, adr_ids={ctx.adr_snapshot[0].adr_id}, settings=settings
    )
    assert not bundle.evidence and not bundle.policy_observations and bundle.limitations


async def test_quote_pool_is_lossless_and_role_checks_still_apply():
    ctx, source = await fixture(policy="Events must follow durable commit.")
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    await evaluate(ctx, source, model)
    request = model.requests[0]
    output = AssessmentBatchOutput.model_validate_json(
        (await model.assess(request)).output_json
    )
    wire = compact(output, request)
    assert wire.expanded(request) == output
    assert len(wire.model_dump_json()) < len(output.model_dump_json())
    proposal = next(u for u in output.units if u.issues)
    unit = next(u for u in request.units if u.unit_id == proposal.unit_id)
    issue = proposal.issues[0]
    policy = refs(request, unit, "ADR_DOCUMENT")
    mixed = issue.model_copy(
        update={
            "revisions": tuple(
                r.model_copy(update={"citations": r.citations + policy})
                for r in issue.revisions
            )
        }
    )
    with pytest.raises(ValueError, match="WRONG_REVISION_CITATION"):
        validate_issue(proposal, mixed, request, unit, "test")


async def test_challenge_input_keeps_each_issue_once_with_all_original_evidence():
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    await evaluate(ctx, source, model)
    request = model.challenge_requests[0]
    before = request.model_dump_json()
    data = json.loads(provider_request_json(request, "CHALLENGE"))
    assert request.model_dump_json() == before
    from ai_services.decision_guard.evaluation.challenge import expand_challenge_sources

    restored = {e.evidence_id: e for e in expand_challenge_sources(data)}
    original = {e.evidence_id: e for e in request.assessment.evidence}
    assert all(original[eid] == e for eid, e in restored.items())
    assert all(
        e.evidence_id in restored
        for e in request.assessment.evidence
        if e.origin != "INDEXED_CODE"
    )
    coverage = data["coverage"]
    assert coverage["adr_retrieval"] == request.assessment.adr_retrieval
    assert coverage["total_details"] == len(request.assessment.limitations)
    assert all("proposal" not in c for c in data["candidates"])
    assert len(data["candidates"]) == len(request.candidates)
    assert all(c["source_claims"]["core"] for c in data["candidates"])
    assert len(provider_request_json(request, "CHALLENGE")) < len(before)


@pytest.mark.parametrize("mode", ["unknown", "duplicate"])
async def test_quote_pool_rejects_unbound_or_redundant_definitions(mode):
    ctx, source = await fixture()
    model = ScriptedModel()
    await evaluate(ctx, source, model)
    request = model.requests[0]
    output = AssessmentBatchOutput.model_validate_json(
        (await model.assess(model.requests[0])).output_json
    )
    data = compact(output, request).model_dump(mode="json")
    if mode == "unknown":
        data["units"][0]["support"] = ["unknown"]
    elif mode == "duplicate":
        data["quotes"].append(data["quotes"][0])
    with pytest.raises(ValueError):
        WireAssessment.model_validate_json(json.dumps(data)).expanded(request)


@pytest.mark.parametrize("schema", [WireAssessment, WireChallenge])
def test_compact_provider_schema_remains_strict(schema):
    value = to_strict_json_schema(schema)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", ())) == set(node.get("properties", {}))
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(value)


async def test_binary_policy_text_is_not_attested_as_complete_visual_policy(tmp_path):
    from ai_services.decision_guard.models import digest

    policy = "Events must follow durable commit."
    original = tmp_path / "source.docx"
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + policy
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    data = original.read_bytes()
    # Adapt the isolated test snapshot to the binary document's actual byte identity.
    ctx, _ = await fixture(policy=policy)
    payload = ctx.model_dump(mode="json")
    # Byte SHA, not decoded-text SHA, is the original document identity.
    import hashlib

    byte_hash = hashlib.sha256(data).hexdigest()
    for doc in payload["adr_snapshot"]:
        doc["content_hash"] = byte_hash
    for e in payload["evidence"]:
        if e["kind"] == "ADR":
            e["document_hash"] = byte_hash
    payload["adr_catalog_hash"] = digest(
        json.dumps(payload["adr_snapshot"], sort_keys=True)
    )
    payload["context_hash"] = digest(
        json.dumps(
            {k: v for k, v in payload.items() if k not in {"context_hash", "budget"}},
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    from ai_services.decision_guard.models import ArchitectureContext

    ctx = ArchitectureContext.model_validate_json(json.dumps(payload))
    authority, record, path = store_policy(tmp_path, policy, ctx)
    target = path.with_suffix(".docx")
    target.write_bytes(data)
    record.file_path = str(target)
    record.source_name = "policy.docx"
    bundle = authority.policy_evidence(
        ctx, adr_ids={ctx.adr_snapshot[0].adr_id}, settings=EvaluationSettings()
    )
    assert bundle.policy_observations[0].coverage == "TEXT_PARTIAL"
    assert bundle.evidence[0].completeness == "EXCERPT"
    assert any(l.code == "POLICY_TEXT_PARTIAL" for l in bundle.limitations)


async def test_policy_reader_failure_is_visible_review_not_a_pass():
    ctx, source = await fixture()

    class Failed(TrustedAuthority):
        def policy_evidence(self, *args, **kwargs):
            raise OSError("unavailable")

    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, authority=Failed())
    assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests
    assert any(l.code == "POLICY_SOURCE_UNAVAILABLE" for l in result.limitations)
