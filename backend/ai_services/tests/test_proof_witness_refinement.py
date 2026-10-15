"""Source-grounded synthetic witnesses; scripted semantics, never live accuracy claims."""

import pytest

from ai_services.decision_guard.evaluation.models import (
    EvidenceNeed,
    MissingFact,
    PotentialConsequence,
)
from ai_services.tests.test_decision_guard_context import HEAD, MERGE, changed
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    evaluate,
    fixture,
    refs,
)
from ai_services.tests.test_proof_scoping import (
    AFTER,
    BEFORE,
    RULE,
    WitnessModel,
    edit_output,
)


def domain(enabled=True, incomplete=True):
    return (
        "\nfrom types import SimpleNamespace\ndef permitted_rows():\n    return [SimpleNamespace(enabled=True, done=True), SimpleNamespace(enabled="
        + repr(enabled)
        + ", done="
        + repr(not incomplete)
        + ")]\ndef consumer():\n    return handler(permitted_rows())\n"
    )


def execute_source(text):
    scope = {}
    exec(compile(text, "trusted-test-fixture", "exec"), scope)  # noqa: S102 -- fixed independent test sources only
    rows = scope["permitted_rows"]()
    return scope["consumer"](), any(r.enabled and not r.done for r in rows)


class GroundedModel(WitnessModel):
    def __init__(self, before, head):
        super().__init__()
        self.before, self.head = before, head

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)

        def transform(u):
            return u.model_copy(
                update={
                    "issues": tuple(
                        i.model_copy(
                            update={
                                "claim": "The supplied consumer can return readiness for its permitted mixed enabled task state",
                                "core": i.core.model_copy(
                                    update={
                                        "boundary": "consumer readiness return",
                                        "witness": "The two rows returned by the original permitted_rows factory",
                                        "qualifiers": (
                                            "Factory permits enabled completed and enabled incomplete rows on this call path",
                                        ),
                                    }
                                ),
                            }
                        )
                        for i in u.issues
                    )
                }
            )

        return edit_output(reply, transform)

    async def challenge(self, request, **kwargs):
        old, _old_bad = execute_source(self.before)
        new, new_bad = execute_source(self.head)
        self.challenge_verdict = (
            "SUPPORTED" if not old and new and new_bad else "REFUTED"
        )
        return await ScriptedModel.challenge(self, request, **kwargs)


@pytest.mark.parametrize(
    "enabled,incomplete,confirms",
    [(True, True, True), (False, True, False), (True, False, False)],
)
async def test_universal_rule_needs_one_qualifying_reachable_witness(
    enabled, incomplete, confirms
):
    before = BEFORE + domain(enabled, incomplete)
    head = AFTER + domain(enabled, incomplete)
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", before, head)]
    )
    model = GroundedModel(before, head)
    result = await evaluate(ctx, source, model)
    assert (
        any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings) == confirms
        and len(model.challenge_requests) == 1
    )
    if confirms:
        assert result.findings[0].disposition == "SUPPORTED_CONFLICT"


async def test_constructible_but_unreachable_counterexample_is_refuted():
    # The supplied consumer always gets completed rows, despite a claimed mixed witness.
    before = BEFORE + domain(incomplete=False)
    head = AFTER + domain(incomplete=False)
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", before, head)]
    )
    model = GroundedModel(before, head)
    result = await evaluate(ctx, source, model)
    assert (
        all(f.disposition == "REFUTED" for f in result.findings)
        and model.challenge_verdict == "REFUTED"
    )


async def test_genuinely_unknown_reachability_is_core_uncertainty():
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class Unknown(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            fact = MissingFact(
                fact_id="reachable",
                scope="CORE",
                fact="Whether the claimed caller can supply this qualifying state",
                relevance="Could make the witness unreachable",
                unrequestable_reason="No source-backed caller domain is supplied",
            )
            return edit_output(
                reply,
                lambda u: u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(update={"missing_facts": (fact,)})
                            for i in u.issues
                        )
                    }
                ),
            )

    model = Unknown()
    result = await evaluate(ctx, source, model)
    assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests


async def test_unrelated_preexisting_defect_does_not_block_same_witness_comparison():
    legacy = "\ndef unrelated_bug(rows):\n    return any(r.done for r in rows)\n"
    before = BEFORE + domain() + legacy
    head = AFTER + domain() + legacy
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", before, head)]
    )
    model = GroundedModel(before, head)
    result = await evaluate(ctx, source, model)
    assert (
        result.outcome == "CONFLICTS_FOUND"
        and result.findings[0].attribution == "INTRODUCED"
    )


@pytest.mark.parametrize(
    "role,behavior,eligible",
    [
        ("MERGE_BASE", "VIOLATES", False),
        ("TARGET_BASE", "VIOLATES", False),
        ("PR_HEAD", "UNKNOWN", False),
        ("TARGET_BASE", "UNKNOWN", False),
        ("TARGET_BASE", "SATISFIES", True),
    ],
)
async def test_three_revision_attribution_concerns_same_allegation(
    role, behavior, eligible
):
    before = AFTER if role == "MERGE_BASE" and behavior == "VIOLATES" else BEFORE
    target = AFTER if role == "TARGET_BASE" and behavior == "VIOLATES" else BEFORE
    ctx, source = await fixture(
        policy=RULE,
        files=[
            changed("service.py", before + domain(), AFTER + domain() + "# edited\n")
        ],
        merge=MERGE,
        target=target + domain(),
    )

    class RevisionModel(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            return edit_output(
                reply,
                lambda u: u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(
                                update={
                                    "revisions": tuple(
                                        r.model_copy(update={"behavior": behavior})
                                        if r.revision == role
                                        else r
                                        for r in i.revisions
                                    )
                                }
                            )
                            for i in u.issues
                        )
                    }
                ),
            )

    model = RevisionModel()
    result = await evaluate(ctx, source, model)
    assert bool(model.challenge_requests) == eligible
    if not eligible:
        assert (
            result.outcome == "REVIEW_REQUIRED"
            and result.findings[0].attribution == "UNKNOWN"
        )


@pytest.mark.parametrize(
    "fact_scope,boundary,confirms",
    [
        ("CONSEQUENCE", "handler readiness return", True),
        ("CORE", "completed downstream execution", False),
    ],
)
async def test_downstream_gap_depends_on_alleged_boundary(
    fact_scope, boundary, confirms
):
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class Downstream(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            fact = MissingFact(
                fact_id="consumer",
                scope=fact_scope,
                fact="Whether the downstream executor accepts the value",
                relevance="Necessary only for completed downstream execution",
                unrequestable_reason="No executor source is supplied",
            )
            return edit_output(
                reply,
                lambda u: u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(
                                update={
                                    "core": i.core.model_copy(
                                        update={"boundary": boundary}
                                    ),
                                    "missing_facts": (fact,),
                                    "potential_consequences": (
                                        PotentialConsequence(
                                            claim="Downstream may execute",
                                            condition="Consumer accepts value",
                                        ),
                                    ),
                                }
                            )
                            for i in u.issues
                        )
                    }
                ),
            )

    model = Downstream()
    result = await evaluate(ctx, source, model)
    assert bool(model.challenge_requests) == confirms
    assert result.outcome == ("CONFLICTS_FOUND" if confirms else "REVIEW_REQUIRED")


async def test_repeated_already_covered_need_is_reported_but_never_cleared():
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class Repeated(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                if not u.issues:
                    return u
                unit = next(v for v in request.units if v.unit_id == u.unit_id)
                ref = refs(request, unit, "PR_HEAD")[0]
                fact = MissingFact(
                    fact_id="f",
                    scope="CORE",
                    fact="A guarantee absent from this complete file",
                    relevance="Could negate the specific alleged path",
                    anchor_id=ref.evidence_id,
                    revision="PR_HEAD",
                )
                need = EvidenceNeed(
                    anchor_id=ref.evidence_id, revision="PR_HEAD", fact_id="f"
                )
                return u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(update={"missing_facts": (fact,)})
                            for i in u.issues
                        ),
                        "needs": (need, need),
                    }
                )

            return edit_output(reply, transform)

    model = Repeated()
    result = await evaluate(ctx, source, model)
    assert (
        len(model.requests) == 1
        and not model.challenge_requests
        and result.outcome == "REVIEW_REQUIRED"
    )
    diagnostics = [
        l for l in result.limitations if l.code == "EVIDENCE_NEED_ALREADY_COVERED"
    ]
    assert len(diagnostics) == 1 and diagnostics[0].paths == ("service.py",)
    assert (
        len(
            [
                call
                for call in source.calls
                if call[1] == HEAD and call[2] == "service.py"
            ]
        )
        == 1
    )


async def test_reasoned_supplied_fact_does_not_need_retrieval():
    before = BEFORE + domain()
    head = AFTER + domain()
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", before, head)]
    )
    model = GroundedModel(before, head)
    result = await evaluate(ctx, source, model)
    assert len(model.requests) == 1 and len(model.challenge_requests) == 1
    assert not any(
        l.code == "EVIDENCE_NEED_ALREADY_COVERED" for l in result.limitations
    )


@pytest.mark.parametrize(
    "mode", ["unknown_head", "applicability_disagrees", "causality_disagrees"]
)
async def test_support_labels_cannot_override_disagreement(mode):
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class Disagree(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                if not u.issues:
                    return u
                updates = {}
                if mode == "applicability_disagrees":
                    updates["applicability"] = "UNCERTAIN"
                issues = []
                for i in u.issues:
                    values = {}
                    if mode == "unknown_head":
                        values["revisions"] = tuple(
                            r.model_copy(
                                update={
                                    "behavior": "UNKNOWN",
                                    "citations": (),
                                    "observation_ids": (),
                                }
                            )
                            if r.revision == "PR_HEAD"
                            else r
                            for r in i.revisions
                        )
                    if mode == "causality_disagrees":
                        values["attribution"] = "UNKNOWN"
                    issues.append(i.model_copy(update=values))
                return u.model_copy(update={**updates, "issues": tuple(issues)})

            return edit_output(reply, transform)

    model = Disagree()
    result = await evaluate(ctx, source, model)
    assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests


async def test_multiple_allegations_keep_independent_core_sufficiency():
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class Multiple(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                if not u.issues:
                    return u
                local = u.issues[0]
                gap = MissingFact(
                    fact_id="remote",
                    scope="CORE",
                    fact="Remote completed behavior",
                    relevance="Necessary for remote allegation",
                    unrequestable_reason="Remote consumer has no authorized source",
                )
                remote = local.model_copy(
                    update={
                        "claim": "Remote executor completes prematurely",
                        "core": local.core.model_copy(
                            update={"boundary": "Remote completion"}
                        ),
                        "missing_facts": (gap,),
                    }
                )
                return u.model_copy(update={"issues": (local, remote)})

            return edit_output(reply, transform)

    model = Multiple()
    result = await evaluate(ctx, source, model)
    assert (
        len(model.challenge_requests) == 1
        and len(model.challenge_requests[0].candidates) == 1
    )
    assert {f.disposition for f in result.findings} == {
        "SUPPORTED_CONFLICT",
        "NEEDS_REVIEW",
    }


@pytest.mark.parametrize("mode", ["complete", "partial", "documented_exception"])
async def test_policy_clearance_uses_original_complete_scope(mode):
    from ai_services.tests.test_decision_guard_evaluation import TrustedAuthority

    before = BEFORE + domain()
    head = AFTER + domain()
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", before, head)]
    )

    class Authority(TrustedAuthority):
        def policy_evidence(self, *args, **kwargs):
            bundle = super().policy_evidence(*args, **kwargs)
            if mode == "partial":
                return bundle.model_copy(
                    update={
                        "policy_observations": tuple(
                            o.model_copy(update={"coverage": "TEXT_PARTIAL"})
                            for o in bundle.policy_observations
                        )
                    }
                )
            return bundle

    model = GroundedModel(before, head)
    if mode == "documented_exception":
        # The nonqualifying disabled task is explicitly exempt in original RULE.
        before = BEFORE + domain(enabled=False)
        head = AFTER + domain(enabled=False)
        ctx, source = await fixture(
            policy=RULE, files=[changed("service.py", before, head)]
        )
        model = GroundedModel(before, head)
    result = await evaluate(ctx, source, model, authority=Authority())
    if mode == "complete":
        assert result.outcome == "CONFLICTS_FOUND"
    elif mode == "partial":
        assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests
        assert result.findings[0].disposition == "NEEDS_REVIEW"
    else:
        assert (
            all(f.disposition == "REFUTED" for f in result.findings)
            and model.challenge_verdict == "REFUTED"
        )
