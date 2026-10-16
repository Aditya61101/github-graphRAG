"""Stage 4.2 evidence-limits hardening offline regression test suite.

Verifies:
1. Authentic spans longer than 1,000 characters are accepted without rejection.
2. Large ADR documents and large code evidence within resource bounds.
3. Repeated source text and precise span identity across multiple lines.
4. Strict validation of bounds, foreign evidence, and wrong revisions.
5. Legitimate security/resource limits (MAX_SPAN_CHARACTERS = 65,536).
6. Multiple findings and partial coverage preservation.
7. Challenge packet planning and budget exhaustion mechanics.
8. Final-result integrity (no false clean outcomes, conservative semantics).
9. Full production replay of unchanged call_09 response confirming ADR and RKG.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import tiktoken
from pydantic import ValidationError

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.challenge import (
    _catalog,
    _lines,
    expand_challenge,
    resolve_span,
)
from ai_services.decision_guard.evaluation.engine import (
    Pending,
    RequestBudget,
    plan_challenges,
)
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    MAX_SPAN_CHARACTERS,
    AssessmentRequest,
    ChallengeBatchOutput,
    ChallengeDecision,
    ChallengeRequest,
    EvaluationResult,
    ModelReply,
    PreservedAssessment,
    QuoteRef,
)
from ai_services.decision_guard.evaluation.validation import (
    validate_issue,
    validate_review,
    validate_support,
)
from ai_services.decision_guard.evaluation.wire import (
    NamedSpan,
    WireChallenge,
    source_references,
)
from ai_services.decision_guard.models import (
    ArchitectureContext,
    EvidenceSnapshot,
    SourceLocation,
    digest,
    stable_id,
)
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

BACKEND_DIR = Path(__file__).resolve().parents[2]
STAGE_4_2_ROOT = (
    ROOT if (ROOT / "call_09.provider.json").exists()
    else (BACKEND_DIR / "data" / "stage_4_2_validation")
)
from ai_services.tests.test_challenger_hardening import span_request
from ai_services.tests.test_decision_guard_evaluation import (
    AFTER,
    BEFORE,
    ScriptedModel,
    TrustedAuthority,
    changed,
    evaluate,
    fixture,
    tokens,
)
from ai_services.tests.test_challenge_only_validation import (
    ChallengeOnly,
    archived,
)


# ---------------------------------------------------------------------------
# 1. Authentic spans longer than 1,000 characters
# ---------------------------------------------------------------------------


def test_authentic_span_longer_than_1000_chars_is_accepted():
    """Verify spans > 1000 chars are accepted and presentation helpers function."""
    long_text = "Rule line: " + "x" * 1450 + "\n"
    assert len(long_text) > 1000
    request = span_request(long_text)
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
        request,
    )
    assert len(ref.quote) == len(long_text)
    assert ref.quote == long_text

    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    citation = resolve_quote(ref, evidence)
    assert citation.quote == long_text
    assert citation.start_char == 0
    assert citation.end_char == len(long_text)

    # Presentation helpers
    assert len(citation.display_excerpt) == 1003  # 1000 + "..."
    assert citation.display_excerpt.endswith("...")
    assert citation.format_excerpt(50).endswith("...")
    assert len(citation.format_excerpt(50)) <= 53
    assert citation.format_excerpt(2000) == long_text


# ---------------------------------------------------------------------------
# 2. Large ADR documents and large code evidence
# ---------------------------------------------------------------------------


def test_large_adr_documents_and_large_code_evidence():
    """Verify large documents up to 64KB read limits allow span resolution across boundaries."""
    paragraphs = [f"Section {i}: " + ("content " * 50) + "\n" for i in range(40)]
    large_doc = "".join(paragraphs)
    assert 10000 < len(large_doc) < MAX_SPAN_CHARACTERS

    request = span_request(large_doc)
    # Span covering section 5 to 15 (thousands of chars)
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=5, end_line=15),
        request,
    )
    expected_slice = "".join(paragraphs[4:15])
    assert ref.quote == expected_slice
    assert len(ref.quote) > 2000

    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    citation = resolve_quote(ref, evidence)
    assert citation.quote == expected_slice
    assert citation.location.start_line == 14
    assert citation.location.end_line == 24


# ---------------------------------------------------------------------------
# 3. Repeated source text and precise span identity
# ---------------------------------------------------------------------------


def test_repeated_source_text_and_precise_span_identity():
    """Verify repeated identical lines are uniquely resolved via line coordinates."""
    repeated = "common_config = True\n" * 10
    request = span_request(repeated)
    # Target line 7 specifically
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=7, end_line=7),
        request,
    )
    assert ref.quote == "common_config = True\n"
    line_len = len("common_config = True\n")
    assert ref.start_char == 6 * line_len
    assert ref.end_char == 7 * line_len

    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    citation = resolve_quote(ref, evidence)
    assert citation.location.start_line == 16
    assert citation.location.end_line == 16


# ---------------------------------------------------------------------------
# 4. Invalid bounds, foreign evidence, and wrong revisions
# ---------------------------------------------------------------------------


def test_invalid_bounds_remain_strictly_rejected():
    request = span_request("short\n")
    # Out of line bounds
    with pytest.raises(ValueError, match="INVALID_SOURCE_SPAN"):
        resolve_span(
            NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=5),
            request,
        )
    # Column out of line bounds
    with pytest.raises(ValueError, match="INVALID_SOURCE_SPAN"):
        resolve_span(
            NamedSpan(
                span_id="H1",
                source_ref="PR_HEAD:1",
                start_line=1,
                end_line=1,
                start_column=10,
            ),
            request,
        )


def test_foreign_evidence_remains_strictly_rejected():
    request = span_request("valid code\n")
    request.assessment.evidence = tuple(
        e.model_copy(update={"repository_id": "foreign-repo"})
        for e in request.assessment.evidence
    )
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_IDENTITY"):
        source_references(request)


def test_wrong_role_prefix_remains_strictly_rejected():
    request = span_request("valid code\n")
    # Span ID M1 with PR_HEAD source ref
    with pytest.raises(ValueError, match="SPAN_SOURCE_ROLE_MISMATCH"):
        resolve_span(
            NamedSpan(span_id="M1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
            request,
        )


# ---------------------------------------------------------------------------
# 5. Legitimate security/resource limits
# ---------------------------------------------------------------------------


def test_legitimate_security_resource_limits():
    """Verify spans exceeding MAX_SPAN_CHARACTERS (65,536) are rejected with SPAN_TOO_LARGE."""
    oversized = "x" * (MAX_SPAN_CHARACTERS + 1)
    request = span_request(oversized)
    with pytest.raises(ValueError, match="SPAN_TOO_LARGE"):
        resolve_span(
            NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
            request,
        )

    # QuoteRef Pydantic contract also enforces MAX_SPAN_CHARACTERS
    with pytest.raises(ValidationError):
        QuoteRef(evidence_id="ev1", quote=oversized, start_char=0, end_char=len(oversized))


# ---------------------------------------------------------------------------
# 6. Multiple findings and partial coverage preservation
# ---------------------------------------------------------------------------


async def test_multiple_findings_and_partial_coverage():
    """Verify independent candidate review: valid finding confirmed, sibling refuted finding handled cleanly."""
    files = [
        changed("service.py", BEFORE, AFTER),
        changed("handler.py", BEFORE, AFTER),
    ]
    ctx, source = await fixture(files=files)

    class MultiModel(ScriptedModel):
        async def challenge(self, request, **kwargs):
            reply = await super().challenge(request, **kwargs)
            out = ChallengeBatchOutput.model_validate_json(reply.output_json)
            new_decs = []
            for d in out.decisions:
                cand = next(c for c in request.candidates if c.proposal_id == d.proposal_id)
                unit = next(u for u in request.assessment.units if u.unit_id == cand.unit_id)
                if unit.path == "handler.py":
                    new_decs.append(d.model_copy(update={"verdict": "REFUTED"}))
                else:
                    new_decs.append(d)
            return reply.model_copy(
                update={
                    "output_json": ChallengeBatchOutput(
                        batch_id=out.batch_id, decisions=tuple(new_decs)
                    ).model_dump_json()
                }
            )

    result = await evaluate(
        ctx,
        source,
        MultiModel({("ADR", "service.py"): "conflict", ("ADR", "handler.py"): "conflict"}),
    )
    assert result.outcome == "CONFLICTS_FOUND"
    dispositions = {f.disposition for f in result.findings}
    assert "SUPPORTED_CONFLICT" in dispositions
    assert "REFUTED" in dispositions


# ---------------------------------------------------------------------------
# 7. Challenge packet planning and budget exhaustion
# ---------------------------------------------------------------------------


async def test_budget_exhaustion_produces_unreviewed_budget_not_silent_loss():
    """Verify candidates exceeding challenge budget become UNREVIEWED_BUDGET, not silently dropped."""
    ctx, source, preserved, _ = await archived()
    # Preserved assessment already has findings. Set max_tokens to 100 to exhaust budget for challenge
    settings = EvaluationSettings(max_attempts=1, max_tokens=100, retries=0)
    evaluator = DecisionIntegrityEvaluator(
        ChallengeOnly(),
        source=source,
        authority=TrustedAuthority(),
        settings=settings,
        token_counter=tokens,
    )
    result = await evaluator.evaluate(ctx, preserved_assessment=preserved)
    assert result.outcome == "REVIEW_REQUIRED"
    for finding in result.findings:
        assert finding.disposition == "NEEDS_REVIEW"
        assert finding.challenge_state == "UNREVIEWED_BUDGET"
        assert finding.review_error == "EVALUATION_BUDGET"


# ---------------------------------------------------------------------------
# 8. Final-result integrity and no false clean outcomes
# ---------------------------------------------------------------------------


async def test_no_false_clean_outcome_when_uncertain():
    """Verify NO_CONFLICTS_FOUND is never returned when findings remain uncertain."""
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, challenge="INSUFFICIENT")
    result = await evaluate(ctx, source, model)
    assert result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert result.findings[0].challenge_state == "CHALLENGED"
    assert result.findings[0].review_error == "INSUFFICIENT_INDEPENDENT_REVIEW"


# ---------------------------------------------------------------------------
# 9. Preserved call_09 production replay
# ---------------------------------------------------------------------------


async def test_preserved_call_09_production_replay():
    """Offline replay of the exact call_09 Azure response through production evaluator.

    Verifies:
    - Both ADR finding (previously rejected for >1,000 char span) and RKG finding
      are accepted as SUPPORTED_CONFLICT with INTRODUCED attribution.
    - Final result outcome is CONFLICTS_FOUND, execution is COMPLETED, scope is PARTIAL.
    - 2 assessed units, 0 uncertain units.
    - EvaluationResult schema round-trip passes with 0 validation errors.
    """
    required = [
        'A_unified_v7_certified_offline_context.json', 'call_08.request.json',
        'call_08.reply.json', 'call_08.provider.json', 'call_09.provider.json', 'manifest.json',
    ]
    if not all((STAGE_4_2_ROOT / name).is_file() for name in required):
        pytest.skip('Requires preserved Stage 4.2 replay artifacts; no production data is fetched')
    ctx = ArchitectureContext.model_validate_json(
        (STAGE_4_2_ROOT / "A_unified_v7_certified_offline_context.json").read_text()
    )
    original = AssessmentRequest.model_validate_json(
        (STAGE_4_2_ROOT / "call_08.request.json").read_text()
    )
    reply_ass = ModelReply.model_validate_json(
        (STAGE_4_2_ROOT / "call_08.reply.json").read_text()
    )

    provider_chall = json.loads((STAGE_4_2_ROOT / "call_09.provider.json").read_text())
    wire_content = provider_chall["choices"][0]["message"]["content"]
    wire = WireChallenge.model_validate_json(wire_content)

    class Call09ReplayChallenger:
        deployment_revision = "aicloud-gpt-5.4"

        async def assess(self, *args, **kwargs):
            raise NotImplementedError("Assessment call must not execute during replay")

        async def challenge(self, req, **kwargs):
            output = wire.expanded(req)
            return ModelReply(
                output_json=output.model_dump_json(),
                status="OK",
                input_tokens=provider_chall["usage"]["prompt_tokens"],
                output_tokens=provider_chall["usage"]["completion_tokens"],
                request_id=provider_chall["id"],
                wire_output_json=wire_content,
            )

    manifest = json.loads((STAGE_4_2_ROOT / "manifest.json").read_text())
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(ctx.revision.repository_id, Path(case["source_root"]))
    db_path = str(BACKEND_DIR / "data" / "decisionguard.db")
    adrs_path = str(BACKEND_DIR / "data" / "adrs")
    authority = UploadAuthority(ReadOnlyStore(db_path), adrs_path)
    settings = EvaluationSettings.model_validate(manifest["settings"]).model_copy(
        update={
            "max_attempts": 1,
            "max_tokens": 25000,
            "max_input_tokens": 20000,
            "max_output_tokens": 5000,
            "retries": 0,
            "concurrency": 1,
        }
    )
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate", lambda s: len(enc.encode(s, disallowed_special=()))
    )
    preserved = PreservedAssessment(
        request=original,
        reply=reply_ass,
        provider_response_sha256=hashlib.sha256(
            (STAGE_4_2_ROOT / "call_08.provider.json").read_bytes()
        ).hexdigest(),
    )

    evaluator = DecisionIntegrityEvaluator(
        Call09ReplayChallenger(),
        source=source,
        authority=authority,
        settings=settings,
        token_counter=counter,
    )
    result = await evaluator.evaluate(ctx, preserved_assessment=preserved)

    # Core evaluation outcome checks
    assert result.outcome == "CONFLICTS_FOUND"
    assert result.execution == "COMPLETED"
    assert result.coverage.scope == "PARTIAL"
    assert result.coverage.assessed == 2
    assert result.coverage.uncertain == 0
    assert result.coverage.omitted == 0
    assert len(result.findings) == 2

    # Finding dispositions
    findings = {f.kind: f for f in result.findings}
    rkg = findings["RKG_REGRESSION"]
    adr = findings["ADR_CONFLICT"]

    assert rkg.disposition == "SUPPORTED_CONFLICT"
    assert rkg.attribution == "INTRODUCED"
    assert rkg.review_error is None
    assert rkg.review_rejection is None

    assert adr.disposition == "SUPPORTED_CONFLICT"
    assert adr.attribution == "INTRODUCED"
    assert adr.review_error is None
    assert adr.review_rejection is None

    # Check the authentic >1,000 char ADR citation is preserved
    adr_policy_citation = next(c for c in adr.citations if len(c.quote) > 1000)
    assert len(adr_policy_citation.quote) == 1460
    assert "## Architectural Constraints" in adr_policy_citation.quote
    assert adr_policy_citation.display_excerpt.endswith("...")
    assert len(adr_policy_citation.display_excerpt) == 1003

    # Unit dispositions
    assert all(u.status == "ASSESSED" and not u.reason_codes for u in result.units)

    # Full schema validation round-trip
    serialized = result.model_dump_json()
    assert EvaluationResult.model_validate_json(serialized) == result
