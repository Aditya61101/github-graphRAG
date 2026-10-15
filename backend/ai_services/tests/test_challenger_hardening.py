"""Adversarial review via executed independent sources and production span adapter."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.evaluation.challenge import (
    challenge_projection,
    expand_challenge_sources,
    resolve_span,
)
from ai_services.decision_guard.evaluation.evidence import resolve_quote
from ai_services.decision_guard.evaluation.models import (
    MAX_SPAN_CHARACTERS,
    AssessmentBatchOutput,
    ChallengeBatchOutput,
    ChallengeDecision,
    EvaluationResult,
    EvaluationSettings,
    MissingFact,
    RevisionAssessment,
)
from ai_services.decision_guard.evaluation.wire import (
    NamedSpan,
    WireChallenge,
    compact,
    source_references,
)
from ai_services.decision_guard.models import SourceLocation, digest
from ai_services.tests.test_decision_guard_context import MERGE
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    changed,
    evaluate,
    fixture,
    refs,
)
from ai_services.tests.test_response_contract_robustness import (
    OfflineClient,
    adapter,
    source,
)

RULE = "Decision: readiness must be false while any governed item has not passed validation. Diagnostic mode is exempt."


def program(
    any_ready=False, *, safeguard=False, factory=False, unknown=False, marker=""
):
    return (
        f"# {marker}\n# Contract: all governed items must pass before readiness.\n"
        "def allowed_rows(items):\n"
        + (
            "    return [SimpleNamespace(governed=True, passed=True)]\n"
            if factory
            else "    return items\n"
        )
        + "def handler(items):\n    items = allowed_rows(items)\n"
        + (
            "    return external_gate(items)\n"
            if unknown
            else (
                "    if any(i.governed and not i.passed for i in items):\n        return False\n"
                if safeguard
                else ""
            )
            + "    statuses = {i.passed for i in items}\n    return "
            + ("True in statuses\n" if any_ready else "statuses == {True}\n")
        )
    )


def execute(original, rows):
    scope = {"SimpleNamespace": SimpleNamespace}
    exec(compile(original, "independent-original-test-source", "exec"), scope)  # noqa: S102 -- fixed synthetic sources only
    try:
        effective = scope["allowed_rows"](rows)
        return scope["handler"](rows), effective
    except NameError:
        return None, ()


class AdversarialModel(ScriptedModel):
    """Only a test oracle: verdicts derive from original program execution."""

    def __init__(
        self,
        *,
        channels=("ADR",),
        rows=None,
        mode="ordinary",
        edit=None,
        paths=("service.py",),
    ):
        super().__init__(
            {(channel, path): "conflict" for path in paths for channel in channels}
        )
        self.rows = rows or [
            SimpleNamespace(governed=True, passed=True),
            SimpleNamespace(governed=True, passed=False),
        ]
        self.mode, self.edit, self.observed = mode, edit, []

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)
        data = AssessmentBatchOutput.model_validate_json(reply.output_json)
        proposals = []
        for proposal in data.units:
            issues = []
            for issue in proposal.issues:
                core = issue.core.model_copy(
                    update={
                        "boundary": "handler readiness return",
                        "witness": repr([(r.governed, r.passed) for r in self.rows]),
                        "qualifiers": ("Both proposed input rows are governed",),
                        "reachability": "Public handler receives the proposed input, subject to allowed_rows and source guards",
                    }
                )
                issues.append(issue.model_copy(update={"core": core}))
            proposals.append(proposal.model_copy(update={"issues": tuple(issues)}))
        return reply.model_copy(
            update={
                "output_json": data.model_copy(
                    update={"units": tuple(proposals)}
                ).model_dump_json()
            }
        )

    async def challenge(self, request, **kwargs):
        self.challenge_requests.append(request)
        decisions = []
        for candidate in request.candidates:
            unit = next(
                u for u in request.assessment.units if u.unit_id == candidate.unit_id
            )
            observed, behaviors = {}, []
            for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD"):
                original = max(
                    (
                        e.excerpt
                        for e in request.assessment.evidence
                        if e.kind == "CODE"
                        and e.origin == role
                        and e.path == unit.path
                        and e.completeness == "COMPLETE"
                    ),
                    key=len,
                )
                ready, effective = execute(original, self.rows)
                bad = any(r.governed and not r.passed for r in effective)
                observed[role] = (ready, bad)
                behaviors.append(
                    RevisionAssessment(
                        revision=role,
                        behavior="UNKNOWN"
                        if ready is None
                        else "VIOLATES"
                        if ready and bad
                        else "SATISFIES",
                        citations=refs(request.assessment, unit, role),
                    )
                )
            self.observed.append(observed)
            facts = ()
            if any(v[0] is None for v in observed.values()):
                verdict, attribution = "INSUFFICIENT", "UNKNOWN"
                facts = (
                    MissingFact(
                        fact_id="external-" + candidate.proposal_id,
                        scope="CORE",
                        fact="The missing external gate's allowed input domain",
                        relevance="Determines whether this witness can reach the return",
                        unrequestable_reason="No original implementation or authorized path supplied",
                    ),
                )
            elif (
                (
                    self.mode == "diagnostic"
                    and any(
                        "Diagnostic mode is exempt." in e.excerpt
                        for e in request.assessment.evidence
                        if e.kind == "ADR"
                    )
                )
                or not all(r.governed for r in self.rows)
                or observed["PR_HEAD"] != (True, True)
            ):
                verdict, attribution = "REFUTED", candidate.issue.attribution
            elif observed["MERGE_BASE"] == (False, True) and observed[
                "TARGET_BASE"
            ] == (False, True):
                verdict, attribution = "SUPPORTED", "INTRODUCED"
            else:
                verdict, attribution = "INSUFFICIENT", "UNKNOWN"
            citations = (
                refs(request.assessment, unit, "ADR_DOCUMENT")
                if unit.channel == "ADR"
                else refs(request.assessment, unit, "MERGE_BASE")
            ) + refs(request.assessment, unit, "PR_HEAD")
            decisions.append(
                ChallengeDecision(
                    proposal_id=candidate.proposal_id,
                    verdict=verdict,
                    explanation=f"Independent source execution: {observed}; policy exemption mode={self.mode}. Original constraints and effective rows determine qualification, not the assessor label.",
                    tested_witness=candidate.issue.core.witness,
                    witness_correspondence="UNCHANGED",
                    attribution=attribution,
                    citations=citations,
                    revisions=tuple(behaviors),
                    missing_facts=facts,
                )
            )
        wire = compact(
            ChallengeBatchOutput(batch_id=request.batch_id, decisions=tuple(decisions)),
            request,
        )
        data = wire.model_dump(mode="json")
        if self.edit:
            self.edit(data, request)
        wire = WireChallenge.model_validate_json(json.dumps(data))
        return await adapter(OfflineClient(wire)).challenge(request, **kwargs)


async def scenario(*, head=None, before=None, policy=RULE, target=None, **model_args):
    before, head = before or program(), head or program(True)
    ctx, reader = await fixture(
        policy=policy,
        files=[changed("service.py", before, head)],
        target=target,
        **({"merge": MERGE} if target is not None else {}),
    )
    model = AdversarialModel(**model_args)
    result = await evaluate(ctx, reader, model, settings=EvaluationSettings(retries=0))
    return ctx, reader, model, result


@pytest.mark.parametrize(
    "channels,policy", [(("ADR",), RULE), (("RKG",), None), (("ADR", "RKG"), RULE)]
)
async def test_executed_existential_witness_confirms_without_observed_database_rows(
    channels, policy
):
    _, _, model, result = await scenario(channels=channels, policy=policy)
    assert result.outcome == "CONFLICTS_FOUND"
    assert len(model.challenge_requests) == 1
    assert len(model.challenge_requests[0].candidates) == len(channels)
    assert all(
        f.disposition == "SUPPORTED_CONFLICT" and f.challenge_state == "CHALLENGED"
        for f in result.findings
    )
    assert all(
        o["MERGE_BASE"][0] is False and o["PR_HEAD"] == (True, True)
        for o in model.observed
    )
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "kind", ["safeguard", "factory", "qualification", "exception", "unknown"]
)
async def test_adversarial_conditions_are_executed_not_accepted_from_labels(kind):
    head, args = program(True), {}
    if kind == "safeguard":
        head = program(True, safeguard=True)
    if kind == "factory":
        head = program(True, factory=True)
    if kind == "qualification":
        args["rows"] = [
            SimpleNamespace(governed=True, passed=True),
            SimpleNamespace(governed=False, passed=False),
        ]
    if kind == "exception":
        args["mode"] = "diagnostic"
    if kind == "unknown":
        head = program(unknown=True)
    _, _, model, result = await scenario(head=head, **args)
    assert model.observed
    expected = "NEEDS_REVIEW" if kind == "unknown" else "REFUTED"
    assert result.findings[0].disposition == expected
    assert not any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    if kind == "unknown":
        assert result.findings[0].missing_facts[0].scope == "CORE"
        assert result.outcome == "REVIEW_REQUIRED" and result.coverage.uncertain == 1


async def test_target_drift_does_not_inherit_assessor_introduced_label():
    _, _, model, result = await scenario(target=program(True))
    assert model.observed[0]["TARGET_BASE"] == (True, True)
    assert (
        result.outcome == "REVIEW_REQUIRED"
        and result.findings[0].disposition == "NEEDS_REVIEW"
    )


async def test_preexisting_violation_stays_visible_without_challenge():
    rows = [
        SimpleNamespace(governed=True, passed=True),
        SimpleNamespace(governed=True, passed=False),
    ]
    assert execute(program(True), rows)[0] is True
    ctx, reader = await fixture(
        policy=RULE,
        files=[
            changed(
                "service.py", program(True), program(True, marker="unchanged contract")
            )
        ],
    )
    model = AdversarialModel()
    model.attribution = "PRE_EXISTING"
    result = await evaluate(ctx, reader, model)
    assert (
        result.findings[0].disposition == "PRE_EXISTING"
        and not model.challenge_requests
    )


@pytest.mark.parametrize(
    "fault", ["bounds", "columns", "unknown", "role", "revision", "duplicate", "schema"]
)
async def test_bad_span_or_decision_isolated_from_independent_valid_candidate(fault):
    def edit(data, request):
        bad = data["decisions"][1]
        if fault == "schema":
            bad["revisions"][1] = copy.deepcopy(bad["revisions"][0])
            return
        if fault == "revision":
            bad["revisions"][0]["citations"] = list(bad["citations"][-1:])
            return
        original = next(
            s for s in data["spans"] if s["span_id"] == bad["citations"][-1]
        )
        extra = {**original, "span_id": "H9999"}
        if fault == "bounds":
            extra["start_line"] = 0
        if fault == "columns":
            extra["start_column"] = -1
        if fault == "unknown":
            extra["source_ref"] = "PR_HEAD:99999"
        if fault == "role":
            extra["source_ref"] = "MERGE_BASE:1"
        data["spans"].append(extra)
        if fault == "duplicate":
            data["spans"].append(dict(extra))
        bad["citations"].append("H9999")

    _, _, _, result = await scenario(channels=("ADR", "RKG"), edit=edit)
    assert {f.disposition for f in result.findings} == {
        "SUPPORTED_CONFLICT",
        "NEEDS_REVIEW",
    }
    bad = next(f for f in result.findings if f.disposition == "NEEDS_REVIEW")
    assert (
        bad.challenge_state == "REVIEW_INVALID"
        and bad.review_rejection
        and bad.review_rejection.raw_json
    )
    assert bad.candidate and bad.core and result.coverage.uncertain == 1
    assert result.outcome == "CONFLICTS_FOUND" and result.coverage.scope == "PARTIAL"


async def test_a_literal_quote_field_cannot_be_smuggled_into_span_contract():
    def edit(data, request):
        data["spans"][0]["quote"] = "invented ... summary"

    _, _, _, result = await scenario(edit=edit)
    assert result.outcome == "REVIEW_REQUIRED" and result.findings[0].candidate
    assert result.findings[0].challenge_state == "REVIEW_FAILED"


def span_request(text):
    e = source("head-span", text=text).model_copy(
        update={
            "location": SourceLocation(
                path="module.py", start_line=10, end_line=10 + text.count("\n")
            )
        }
    )
    return SimpleNamespace(
        assessment=SimpleNamespace(
            evidence=(e,),
            revision=SimpleNamespace(
                repository_id="repo",
                merge_base_sha="a" * 40,
                base_sha="a" * 40,
                head_sha="a" * 40,
            ),
        )
    )


@pytest.mark.parametrize("text", ["repeat\nrepeat\n", "αβ\r\nαβ\r\n", "...\n...\n"])
def test_explicit_span_preserves_repeated_occurrence_unicode_and_real_ellipsis(text):
    request = span_request(text)
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=2, end_line=2),
        request,
    )
    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    citation = resolve_quote(ref, evidence)
    assert citation.start_char == len(text.splitlines(keepends=True)[0])
    assert (
        citation.quote == text.splitlines(keepends=True)[1]
        and citation.location.start_line == 11
    )


def test_columns_can_select_precisely_within_a_long_original_line():
    request = span_request("x" * 2000 + "λEND\n")
    ref = resolve_span(
        NamedSpan(
            span_id="H1",
            source_ref="PR_HEAD:1",
            start_line=1,
            end_line=1,
            start_column=2000,
            end_column=2004,
        ),
        request,
    )
    assert ref.quote == "λEND" and ref.start_char == 2000


def test_shared_parent_text_does_not_expand_a_snapshot_permitted_span():
    request = span_request("first\nsecond\n")
    full = request.assessment.evidence[0]
    small = full.model_copy(
        update={
            "evidence_id": "small-fragment",
            "excerpt": "second\n",
            "excerpt_hash": digest("second\n"),
            "location": SourceLocation(path=full.path, start_line=11, end_line=11),
        }
    )
    request.assessment.evidence = (full, small)
    alias = next(
        ref
        for ref, e in source_references(request).items()
        if e.evidence_id == small.evidence_id
    )
    with pytest.raises(ValueError, match="SPAN_OUTSIDE_EVIDENCE"):
        resolve_span(
            NamedSpan(span_id="H1", source_ref=alias, start_line=1, end_line=1), request
        )
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref=alias, start_line=2, end_line=2), request
    )
    assert (
        ref.quote == "second\n"
        and ref.evidence_id == small.evidence_id
        and ref.start_char == 0
    )


def test_resolved_bounds_cannot_override_literal_source_identity():
    request = span_request("first\nsecond\n")
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
        request,
    )
    bad = ref.model_copy(update={"start_char": 1, "end_char": 7})
    with pytest.raises(ValueError, match="INVALID_SOURCE_SPAN"):
        resolve_quote(bad, {e.evidence_id: e for e in request.assessment.evidence})


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"start_line": 2, "end_line": 1}, "INVALID_SOURCE_SPAN"),
        ({"end_line": 999}, "INVALID_SOURCE_SPAN"),
        ({"end_column": 999}, "INVALID_SOURCE_SPAN"),
        ({"start_column": 5, "end_column": 2}, "SPAN_OUTSIDE_EVIDENCE"),
        ({"source_ref": "ADR_DOCUMENT:1"}, "UNKNOWN_SOURCE_REF"),
    ],
)
def test_invalid_spans_never_repair_or_substitute_source(changes, reason):
    request = span_request("line1\nline2\n")
    data = {
        "span_id": "H1",
        "source_ref": "PR_HEAD:1",
        "start_line": 1,
        "end_line": 1,
        **changes,
    }
    with pytest.raises(ValueError, match=reason):
        resolve_span(NamedSpan(**data), request)


def test_oversized_or_foreign_source_span_remains_rejected():
    request = span_request("x" * (MAX_SPAN_CHARACTERS + 1))
    with pytest.raises(ValueError, match="SPAN_TOO_LARGE"):
        resolve_span(
            NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
            request,
        )
    request.assessment.evidence = tuple(
        e.model_copy(update={"repository_id": "foreign"})
        for e in request.assessment.evidence
    )
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_IDENTITY"):
        source_references(request)


def test_authentic_span_longer_than_1000_chars_is_accepted():
    long_text = "a" * 1460 + "\n"
    request = span_request(long_text)
    ref = resolve_span(
        NamedSpan(span_id="H1", source_ref="PR_HEAD:1", start_line=1, end_line=1),
        request,
    )
    assert len(ref.quote) == 1461
    assert ref.quote == long_text
    evidence = {e.evidence_id: e for e in request.assessment.evidence}
    citation = resolve_quote(ref, evidence)
    assert citation.quote == long_text
    assert len(citation.display_excerpt) == 1003  # 1000 + "..."
    assert citation.display_excerpt.endswith("...")


async def test_numbered_display_roundtrip_and_tamper_checks():
    _, _, model, _ = await scenario(channels=("ADR", "RKG"))
    req = model.challenge_requests[0]
    before = req.model_dump_json()
    data = challenge_projection(req)
    original = {e.evidence_id: e for e in req.assessment.evidence}
    assert all(original[e.evidence_id] == e for e in expand_challenge_sources(data))
    assert req.model_dump_json() == before and len(data["candidates"]) == 2
    assert "context_records_json" not in data and "candidate_quotes" not in data
    data["source_texts"][0]["numbered_text"] += "999|fabricated\n"
    with pytest.raises(ValueError, match="INVALID_NUMBERED_SOURCE"):
        expand_challenge_sources(data)


async def test_all_eligible_candidates_accounted_when_no_challenge_budget():
    ctx, reader = await fixture(
        policy=RULE, files=[changed("service.py", program(), program(True))]
    )
    model = AdversarialModel(channels=("ADR", "RKG"))
    result = await evaluate(
        ctx, reader, model, settings=EvaluationSettings(max_attempts=1, retries=0)
    )
    assert len(result.findings) == 2 and not model.challenge_requests
    assert all(
        f.challenge_state == "UNREVIEWED_BUDGET" and f.candidate
        for f in result.findings
    )
    assert result.coverage.uncertain == 2 and result.outcome == "REVIEW_REQUIRED"


async def test_multiple_packets_and_partial_execution_are_explicit(monkeypatch):
    from ai_services.decision_guard.evaluation.engine import RequestBudget

    count = RequestBudget.count

    def bounded(self, request, phase):
        return (
            999999
            if phase == "CHALLENGE" and len(request.candidates) > 1
            else count(self, request, phase)
        )

    monkeypatch.setattr(RequestBudget, "count", bounded)
    ctx, reader = await fixture(
        policy=RULE, files=[changed("service.py", program(), program(True))]
    )
    model = AdversarialModel(channels=("ADR", "RKG"))
    result = await evaluate(
        ctx,
        reader,
        model,
        settings=EvaluationSettings(max_attempts=2, retries=0, concurrency=1),
    )
    assert len(model.challenge_requests) == 1 and len(result.findings) == 2
    assert {f.challenge_state for f in result.findings} == {
        "CHALLENGED",
        "UNREVIEWED_BUDGET",
    }
    assert result.coverage.uncertain == 1 and result.coverage.scope == "PARTIAL"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate_json(
            result.model_copy(
                update={"outcome": "NO_CONFLICTS_FOUND"}
            ).model_dump_json()
        )
    model = AdversarialModel(channels=("ADR", "RKG"))
    complete = await evaluate(
        ctx,
        reader,
        model,
        settings=EvaluationSettings(max_attempts=3, retries=0, concurrency=1),
    )
    assert len(model.challenge_requests) == 2 and all(
        f.disposition == "SUPPORTED_CONFLICT" for f in complete.findings
    )
