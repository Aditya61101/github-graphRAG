"""Executable generic witnesses + scripted semantic review; no model clients.

These tests prove orchestration/proof contracts, not live LLM reasoning accuracy.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from ai_services.decision_guard.context import (
    ArchitectureContextBuilder,
    RepositoryScopeError,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    ChallengeBatchOutput,
    CoreAllegation,
    EvidenceNeed,
    MissingFact,
    PotentialConsequence,
)
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    compact,
)
from ai_services.decision_guard.models import digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.tests.test_decision_guard_context import (
    BASE,
    HEAD,
    REPO,
    Graph,
    Readiness,
    Source,
    changed,
    changes,
    chunk,
    entity,
    requirement,
)
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    evaluate,
    fixture,
    refs,
    tokens,
)

RULE = "Accepted decision: readiness may be returned only when every enabled task is complete. Disabled tasks are exempt."
BEFORE = "def handler(rows):\n    return all(r.done for r in rows if r.enabled)\n"
AFTER = "def handler(rows):\n    return any(r.done for r in rows)\n"
SAFE = "def handler(rows):\n    if any(r.enabled and not r.done for r in rows):\n        return False\n    return any(r.done for r in rows)\n"


def execute(text, enabled):
    namespace = {}
    exec(compile(text, "generic-readiness-witness", "exec"), namespace)  # noqa: S102 -- fixed local test fixture, no external source execution
    rows = [
        SimpleNamespace(enabled=True, done=True),
        SimpleNamespace(enabled=enabled, done=False),
    ]
    return namespace["handler"](rows)


def edit_output(reply, transform):
    output = AssessmentBatchOutput.model_validate_json(reply.output_json)
    units = tuple(transform(u) for u in output.units)
    return reply.model_copy(
        update={
            "output_json": output.model_copy(update={"units": units}).model_dump_json()
        }
    )


class WitnessModel(ScriptedModel):
    def __init__(self, *, enabled=True, after=AFTER, policy=True):
        super().__init__({("ADR" if policy else "RKG", "service.py"): "conflict"})
        self.enabled, self.after = enabled, after

    async def assess(self, request, **kwargs):
        reply = await super().assess(request, **kwargs)

        def transform(proposal):
            unit = next(u for u in request.units if u.unit_id == proposal.unit_id)
            issues = tuple(
                i.model_copy(
                    update={
                        "claim": "The helper can return readiness while an enabled task is incomplete",
                        "consequence": "The local readiness return contract is weakened",
                        "core": CoreAllegation(
                            reachability="The qualifying state reaches the source-supported local boundary",
                            boundary="handler readiness return",
                            witness=f"One enabled completed task and one {'enabled' if self.enabled else 'disabled'} incomplete task",
                            qualifiers=("The incomplete task must be enabled",),
                            citations=refs(request, unit, "PR_HEAD"),
                        ),
                    }
                )
                for i in proposal.issues
            )
            return proposal.model_copy(update={"issues": issues})

        return edit_output(reply, transform)

    async def challenge(self, request, **kwargs):
        # Independent test oracle executes the original generic sources. A
        # nonqualifying witness or real pre-return safeguard cannot confirm.
        violating = not execute(BEFORE, self.enabled) and execute(
            self.after, self.enabled
        )
        self.challenge_verdict = "SUPPORTED" if violating else "REFUTED"
        return await super().challenge(request, **kwargs)


@pytest.mark.parametrize(
    "enabled,after,confirmed",
    [(True, AFTER, True), (False, AFTER, False), (True, SAFE, False)],
)
async def test_qualified_witness_and_real_alternate_safeguard(
    enabled, after, confirmed
):
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, after)]
    )
    model = WitnessModel(enabled=enabled, after=after)
    result = await evaluate(ctx, source, model)
    assert (
        bool(any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings))
        == confirmed
    )
    assert len(model.challenge_requests) == 1
    if confirmed:
        assert result.findings[0].core.qualifiers == (
            "The incomplete task must be enabled",
        )
    else:
        assert all(
            f.disposition == "REFUTED" for f in result.findings
        )  # Preserve the evidenced refutation.


@pytest.mark.parametrize("scope", ["CORE", "CONSEQUENCE"])
async def test_narrow_regression_does_not_require_unrelated_downstream(scope):
    ctx, source = await fixture(
        policy=RULE, files=[changed("service.py", BEFORE, AFTER)]
    )

    class UnknownDownstream(WitnessModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                return u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(
                                update={
                                    "missing_facts": (
                                        MissingFact(
                                            fact_id="consumer",
                                            scope=scope,
                                            fact="Final consumer outcome",
                                            relevance="Relevant only if alleging completed downstream execution",
                                            unrequestable_reason="No authorized source anchor for downstream behavior",
                                        ),
                                    ),
                                    "potential_consequences": (
                                        PotentialConsequence(
                                            claim="Downstream dispatch may start early",
                                            condition="The consumer accepts the returned readiness value",
                                        ),
                                    ),
                                }
                            )
                            for i in u.issues
                        )
                    }
                )

            return edit_output(reply, transform)

    model = UnknownDownstream()
    result = await evaluate(ctx, source, model)
    if scope == "CONSEQUENCE":
        assert (
            result.outcome == "CONFLICTS_FOUND" and len(model.challenge_requests) == 1
        )
        assert result.findings[0].potential_consequences[0].condition
    else:
        assert result.outcome == "REVIEW_REQUIRED" and not model.challenge_requests
        assert any(l.code == "CORE_FACT_UNREQUESTABLE" for l in result.limitations)


async def test_rkg_only_core_regression_without_adr():
    ctx, source = await fixture(
        policy=None, files=[changed("service.py", BEFORE, AFTER)]
    )
    model = WitnessModel(policy=False)
    result = await evaluate(ctx, source, model)
    assert not ctx.requirements and not result.authority
    assert result.outcome == "CONFLICTS_FOUND"
    assert result.findings[0].kind == "RKG_REGRESSION"
    assert not any(p.obligation == "NORMATIVE_RULE" for p in result.proofs)


async def test_challenger_cannot_support_with_core_blocking_fact():
    ctx, source = await fixture(policy=RULE)

    class UncertainChallenger(ScriptedModel):
        async def challenge(self, request, **kwargs):
            reply = await super().challenge(request, **kwargs)
            output = ChallengeBatchOutput.model_validate_json(reply.output_json)
            fact = MissingFact(
                fact_id="qualification",
                scope="CORE",
                fact="Witness domain membership",
                relevance="Could make the witness exempt",
                unrequestable_reason="Not evidenced by the original source",
            )
            decisions = tuple(
                d.model_copy(update={"missing_facts": (fact,)})
                for d in output.decisions
            )
            return reply.model_copy(
                update={
                    "output_json": output.model_copy(
                        update={"decisions": decisions}
                    ).model_dump_json()
                }
            )

    model = UncertainChallenger({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert len(model.challenge_requests) == 1 and result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].missing_facts[0].scope == "CORE"


async def test_evidence_need_must_match_fact_and_source_anchor():
    ctx, source = await fixture(policy=RULE)

    class WrongNeed(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                unit = next(x for x in request.units if x.unit_id == u.unit_id)
                ref = refs(request, unit, "PR_HEAD")[0]
                fact = MissingFact(
                    fact_id="rule",
                    scope="CORE",
                    fact="A necessary enforcing rule",
                    relevance="May negate the violation",
                    anchor_id=ref.evidence_id,
                )
                return u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(update={"missing_facts": (fact,)})
                            for i in u.issues
                        ),
                        "needs": (
                            EvidenceNeed(
                                anchor_id=ref.evidence_id,
                                revision="TARGET_BASE",
                                fact_id="rule",
                            ),
                        ),
                    }
                )

            return edit_output(reply, transform)

    model = WrongNeed({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert not model.challenge_requests
    assert any("EVIDENCE_NEED_FACT_MISMATCH" in u.reason_codes for u in result.units)


async def test_supported_final_core_cannot_substitute_policy_for_head_source():
    from ai_services.decision_guard.evaluation.models import EvaluationResult

    ctx, source = await fixture(policy=RULE)
    result = await evaluate(
        ctx, source, ScriptedModel({("ADR", "service.py"): "conflict"})
    )
    data = result.model_dump(mode="json")
    policy = next(e for e in result.evidence if e.kind == "ADR")
    data["findings"][0]["core"]["citations"] = [
        {"evidence_id": policy.evidence_id, "quote": policy.excerpt}
    ]
    with pytest.raises(ValidationError, match="exact changed head"):
        EvaluationResult.model_validate_json(json.dumps(data))


async def test_missing_or_invented_core_cannot_confirm():
    ctx, source = await fixture(policy=RULE)

    class MissingCore(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            return edit_output(
                reply,
                lambda u: u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(update={"core": None}) for i in u.issues
                        )
                    }
                ),
            )

    model = MissingCore({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert (
        result.outcome == "NOT_EVALUATED"
        and result.coverage.errors
        and not model.challenge_requests
    )


@pytest.mark.parametrize("mode", ["anchored", "unrequestable", "invented"])
async def test_actionable_fact_reads_only_authorized_source_and_one_reassessment(mode):
    ctx, source = await fixture(policy=RULE)
    # An original, backend-associated entity path can authorize supplementation
    # before its full revision source is present in the assessment packet.
    e = ctx.entities[0].model_copy(update={"paths": ("adapter.py",)})
    ctx = ctx.model_copy(update={"entities": (e,)})
    payload = ctx.model_dump(mode="json", exclude={"context_hash", "budget"})
    ctx = ctx.model_copy(
        update={
            "context_hash": digest(
                json.dumps(payload, sort_keys=True, ensure_ascii=False)
            )
        }
    )
    source.values[HEAD, "adapter.py"] = "def preserve():\n    return True\n"

    class NeedModel(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)

            def transform(u):
                available = any(
                    o.path == "adapter.py" and o.revision == "PR_HEAD"
                    for o in request.observations
                )
                if available:
                    return u
                kwargs = (
                    {"anchor_id": e.entity_id if mode == "anchored" else "foreign-path"}
                    if mode != "unrequestable"
                    else {"unrequestable_reason": "No original supported anchor"}
                )
                fact = MissingFact(
                    fact_id="safeguard",
                    scope="CORE",
                    fact="Adapter's enforcing contract",
                    relevance="Could prevent this actual violation",
                    **kwargs,
                )
                return u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(update={"missing_facts": (fact,)})
                            for i in u.issues
                        )
                    }
                )

            return edit_output(reply, transform)

    model = NeedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    if mode == "anchored":
        assert len(model.requests) == 2 and len(model.challenge_requests) == 1
        assert result.outcome == "CONFLICTS_FOUND"
        assert len([x for x in source.calls if x[2] == "adapter.py"]) == 1
        assert model.requests[0].units[0].unit_id in next(
            a.unit_ids
            for a in model.requests[0].source_anchors
            if a.anchor_id == e.entity_id
        )
    else:
        assert len(model.requests) == 1 and not model.challenge_requests
        assert not any(x[2] == "adapter.py" for x in source.calls)
        assert result.outcome == "REVIEW_REQUIRED"
        if mode == "invented":
            assert any(
                "UNAUTHORIZED_FACT_ANCHOR" in u.reason_codes for u in result.units
            )


def test_missing_fact_requires_request_or_explicit_unrequestable_reason():
    with pytest.raises(ValidationError, match="anchor or an unrequestable"):
        MissingFact(
            fact_id="x",
            scope="CORE",
            fact="Unknown contract",
            relevance="Could negate the claim",
        )


async def test_wire_roundtrip_keeps_core_facts_and_qualifiers():
    ctx, source = await fixture(policy=RULE)
    model = WitnessModel()
    await evaluate(ctx, source, model)
    reply = await model.assess(model.requests[0])
    output = AssessmentBatchOutput.model_validate_json(reply.output_json)
    assert compact(output, model.requests[0]).expanded(model.requests[0]) == output
    for schema in (WireAssessment, WireChallenge):
        strict = to_strict_json_schema(schema)
        assert strict["additionalProperties"] is False
        assert "title" not in strict


def edge(src, dst, text, path="service.py", kind="CALLS"):
    return {
        "source": src,
        "target": dst,
        "assertion": {
            "id": src["id"] + "|" + kind + "|" + dst["id"],
            "repository": REPO,
            "relationshipType": kind,
            "rationale": "Original code supports this directed relationship",
        },
        "support": [
            {"chunk": chunk(src["id"] + dst["id"], path, text), "is_adr": False}
        ],
    }


async def graph_fixture(
    *, budget=24000, neighbor_limit=12, large=False, bad_declaration=None
):
    graph, source = Graph(), Source()
    main = "def handler(rows):\n    return all(r.done for r in rows)\n"
    subject = entity("subject", "handler")
    reference = entity("reference", "unrelated")
    caller = entity("caller", "invoke")
    domain = entity("domain", "Task")
    caller_text = "def invoke(rows):\n    return handler(rows)\n"
    domain_text = "class Task:\n    enabled = True\n    done = False\n"
    graph.chunks = [
        {
            "path": "service.py",
            "chunk": chunk(text=main),
            "entities": [subject, reference],
        },
        {
            "path": "caller.py",
            "chunk": chunk("caller", "caller.py", caller_text),
            "entities": [caller],
        },
        {
            "path": "domain.py",
            "chunk": chunk("domain", "domain.py", domain_text),
            "entities": [domain],
        },
    ]
    source.values = {
        (BASE, "service.py"): main,
        (HEAD, "service.py"): AFTER,
        (BASE, "caller.py"): caller_text,
        (HEAD, "caller.py"): caller_text,
        (BASE, "domain.py"): domain_text,
        (HEAD, "domain.py"): domain_text,
    }
    graph.edges = [
        edge(caller, subject, caller_text, "caller.py"),
        edge(subject, domain, main, kind="READS_FROM"),
    ]
    for i in range(20):
        other = entity(f"noise{i}", f"unrelated{i}")
        graph.edges.append(edge(reference, other, main))
        # Large source-backed peripheral edges put real pressure on pruning.
        if large:
            noisy = "# " + "irrelevant " * 250 + "\n" + main
            source.values[BASE, f"noise{i}.py"] = noisy
            graph.edges.append(edge(subject, other, noisy, f"noise{i}.py", "USES"))
    graph.knowledge = [requirement("rule", targets=[domain], statement=RULE)]
    graph.documents = [{"adr": graph.knowledge[0]["support"][0]["adr"]}]
    if bad_declaration == "hash":
        graph.chunks[2]["chunk"]["contentHash"] = "0" * 64
    if bad_declaration == "repository":
        graph.chunks[2]["chunk"]["repository"] = "foreign-repository"
    b = ArchitectureContextBuilder(
        graph,
        source,
        Readiness(),
        lambda _: asyncio.Lock(),
        ContextSettings(
            max_context_tokens=budget, max_neighbors_per_subject=neighbor_limit
        ),
        token_counter=tokens,
    )
    return await b.build(changes(changed("service.py", main, AFTER))), graph, source


async def test_relevant_neighbors_and_domain_declaration_retained_without_reference_fanout():
    ctx, graph, source = await graph_fixture()
    assert next(c for c in graph.calls if c[0] == "edges")[1] == ("subject",)
    assert {r.relationship_type for r in ctx.relationships} == {"CALLS", "READS_FROM"}
    assert any(
        e.path == "domain.py" and "enabled = True" in e.excerpt for e in ctx.evidence
    )
    assert ctx.requirements and any(e.kind == "ADR" for e in ctx.evidence)
    assert ctx.budget.estimated_tokens <= 24000
    assert all(c[0] == REPO for c in source.calls)
    assert any(l.code == "RELATIONSHIP_SEED_OMITTED" for l in ctx.coverage.limitations)


async def test_bounded_budget_preserves_core_policy_and_consumers_before_peripheral_groups():
    ctx, _, _ = await graph_fixture(budget=10000, large=True)
    assert ctx.requirements
    assert "subject" in {e.entity_id for e in ctx.entities}
    assert any(r.relationship_type == "CALLS" for r in ctx.relationships)
    assert any(e.path == "domain.py" for e in ctx.evidence)
    assert ctx.budget.estimated_tokens <= 10000
    omissions = [
        l
        for l in ctx.coverage.limitations
        if l.code in {"CONTEXT_TOKEN_LIMIT", "RELATIONSHIP_SELECTION_OMITTED"}
    ]
    assert omissions and all(l.item_ids for l in omissions)


async def test_neighbor_selection_omissions_are_visible():
    ctx, _, _ = await graph_fixture(neighbor_limit=1)
    assert len(ctx.relationships) == 1
    assert any(
        l.code == "RELATIONSHIP_SELECTION_OMITTED" and l.item_ids
        for l in ctx.coverage.limitations
    )


async def test_neighbor_declaration_hash_failure_does_not_authorize_its_path():
    ctx, _, _ = await graph_fixture(bad_declaration="hash")
    assert not any(e.path == "domain.py" for e in ctx.evidence)
    assert any(l.code == "CHUNK_HASH_MISMATCH" for l in ctx.coverage.limitations)
    assert not next(e for e in ctx.entities if e.entity_id == "domain").paths


async def test_neighbor_declaration_cannot_escape_repository():
    with pytest.raises(RepositoryScopeError):
        await graph_fixture(bad_declaration="repository")
