"""Full Stage 4.1 -> 4.2 fixtures with explicit, replayable semantic responses.

These test gates and orchestration, not a claim of real-model reasoning accuracy.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    DecisionIntegrityEvaluator,
    EvaluationInputError,
    EvaluationResult,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.evidence import (
    resolve_quote,
    validate_context,
)
from ai_services.decision_guard.evaluation.models import (
    AssessmentBatchOutput,
    AuthorityReceipt,
    ChallengeBatchOutput,
    ChallengeDecision,
    CoreAllegation,
    EvidenceBundle,
    EvidenceDiscussion,
    ModelReply,
    PolicyObservation,
    ProposedIssue,
    QuoteRef,
    RevisionAssessment,
    UnitProposal,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.tests.test_decision_guard_context import (
    BASE,
    HEAD,
    MERGE,
    REPO,
    Graph,
    Readiness,
    Source,
    changed,
    changes,
    chunk,
    entity,
)


def tokens(text):
    return (len(text.encode("utf-8")) + 3) // 4


tokens.name = "test:utf8-quarter"
POLICY = "Decision: All newly introduced Python local variables must use camelCase, except files under generated/."
BEFORE = "def handler():\n    orderTotal = 1\n    return orderTotal\n"
AFTER = "def handler():\n    order_total = 1\n    return order_total\n"


async def fixture(
    *,
    policy=POLICY,
    policy_document=None,
    files=None,
    merge=BASE,
    target=None,
    edges=(),
    extra_source=None,
    context_settings=None,
):
    files = files or [changed("service.py", BEFORE, AFTER)]
    graph, source, ready = Graph(), Source(), Readiness()
    graph.chunks = []
    source.values = {}
    for i, f in enumerate(files):
        if f.base_content is not None:
            source.values[merge, f.previous_path or f.path] = f.base_content
            source.values[BASE, f.previous_path or f.path] = target or f.base_content
            graph.chunks.append(
                {
                    "path": f.previous_path or f.path,
                    "chunk": chunk(
                        f"c{i}",
                        f.previous_path or f.path,
                        target or f.base_content,
                        BASE,
                    ),
                    "entities": [entity(f"e{i}", "handler")],
                }
            )
        if f.head_content is not None:
            source.values[HEAD, f.path] = f.head_content
    graph.edges = list(edges)
    source.values.update(extra_source or {})
    if policy is not None:
        doc = {
            "id": "adr-1",
            "repository": REPO,
            "content_hash": digest(policy_document or policy),
            "source_type": "MANUAL_UPLOAD",
            "source_name": "policy.md",
        }
        support = {
            "adr": doc,
            "chunk": {
                "id": "adr:policy",
                "repository": REPO,
                "adr_id": "adr-1",
                "text": policy,
                "contentHash": digest(policy),
                "section": "Decision",
            },
        }
        graph.documents = [{"adr": doc}]
        graph.knowledge = [
            {
                "knowledge": {
                    "id": "rule-1",
                    "repository": REPO,
                    "description": policy,
                    "applicability_repository_wide": True,
                },
                "kind": "CONSTRAINT",
                "targets": [],
                "support": [support],
            }
        ]
    builder = ArchitectureContextBuilder(
        graph,
        source,
        ready,
        lambda _: asyncio.Lock(),
        settings=context_settings,
        token_counter=tokens,
    )
    ctx = await builder.build(changes(*files, merge_base_sha=merge))
    return ctx, source


class TrustedAuthority:
    def receipts(self, ctx):
        return tuple(
            AuthorityReceipt(
                repository_id=REPO,
                adr_id=a.adr_id,
                document_hash=a.content_hash,
                owner_id="owner",
                status="APPROVED",
                reason="OWNER_UPLOAD_VERIFIED",
            )
            for a in ctx.adr_snapshot
        )

    def policy_evidence(self, ctx, *, adr_ids, settings):
        # These fixtures define their entire original policy as one known text.
        evidence = tuple(
            e.model_copy(update={"evidence_id": "full-policy:" + e.evidence_id})
            for e in ctx.evidence
            if e.kind == "ADR"
            and e.adr_id in adr_ids
            and digest(e.excerpt) == e.document_hash
        )
        return EvidenceBundle(
            evidence=evidence,
            policy_observations=tuple(
                PolicyObservation(
                    adr_id=e.adr_id,
                    document_hash=e.document_hash,
                    evidence_id=e.evidence_id,
                    coverage="TEXT_COMPLETE",
                )
                for e in evidence
            ),
        )


def refs(request, unit, role):
    evidence = [
        e
        for e in request.evidence
        if e.evidence_id in unit.evidence_ids
        and e.origin == role
        and (
            e.kind == "ADR"
            if role == "ADR_DOCUMENT"
            else e.kind == "CODE" and e.path == unit.path
        )
        and e.excerpt
    ]
    evidence.sort(key=lambda e: -len(e.excerpt))
    return tuple(
        QuoteRef(evidence_id=e.evidence_id, quote=e.excerpt[:1000])
        for e in evidence[:1]
    )


def observations(request, unit, role):
    return tuple(
        o.observation_id
        for o in request.observations
        if o.revision == role and o.path == unit.path and o.status == "ABSENT"
    )


class ScriptedModel:
    deployment_revision = "fixture-v1"

    def __init__(
        self,
        outcomes=None,
        *,
        challenge="SUPPORTED",
        attribution="INTRODUCED",
        invalid=False,
        fail=False,
    ):
        self.outcomes = outcomes or {}
        self.challenge_verdict, self.attribution = challenge, attribution
        self.invalid, self.fail = invalid, fail
        self.requests, self.challenge_requests = [], []

    async def assess(self, request, **kwargs):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        units = []
        for unit in request.units:
            outcome = self.outcomes.get((unit.channel, unit.path), "clean")
            support = refs(request, unit, "ADR_DOCUMENT") + refs(
                request, unit, "PR_HEAD"
            )
            if self.invalid:
                support = (QuoteRef(evidence_id="invented", quote="not present"),)
            issues = ()
            if outcome in {"conflict", "impact"}:
                revision_rows = []
                for role in ("MERGE_BASE", "TARGET_BASE", "PR_HEAD"):
                    behavior = (
                        "VIOLATES"
                        if role == "PR_HEAD"
                        or self.attribution in {"PRE_EXISTING", "WORSENED"}
                        else "SATISFIES"
                    )
                    if observations(request, unit, role):
                        behavior = "NOT_APPLICABLE" if role != "PR_HEAD" else "VIOLATES"
                    revision_rows.append(
                        RevisionAssessment(
                            revision=role,
                            behavior=behavior,
                            citations=refs(request, unit, role),
                            observation_ids=observations(request, unit, role),
                        )
                    )
                issues = (
                    ProposedIssue(
                        kind="ARCHITECTURAL_IMPACT"
                        if outcome == "impact"
                        else "ADR_CONFLICT"
                        if unit.channel == "ADR"
                        else "RKG_REGRESSION",
                        claim="Changed behavior contradicts the evidenced contract",
                        mechanism="The new implementation removes the required behavior",
                        consequence="The affected consumer cannot rely on the previous contract",
                        suggested_action="Restore the required behavior",
                        attribution=self.attribution,
                        revisions=tuple(revision_rows),
                        rule=EvidenceDiscussion(
                            statement="Binding original architectural contract",
                            citations=refs(request, unit, "ADR_DOCUMENT")
                            if unit.channel == "ADR"
                            else refs(request, unit, "MERGE_BASE")
                            or refs(request, unit, "PR_HEAD"),
                        ),
                        counterevidence=EvidenceDiscussion(
                            statement="Reviewed complete policy and local code for applicable exceptions or effective safeguards"
                        ),
                        core=CoreAllegation(
                            boundary="Changed subject's observable contract",
                            reachability="The source function is called at the evidenced boundary",
                            witness="The changed source removes the behavior required by the original contract",
                            citations=(
                                QuoteRef(evidence_id="invented", quote="not present"),
                            )
                            if self.invalid
                            else refs(request, unit, "PR_HEAD"),
                            observation_ids=observations(request, unit, "PR_HEAD"),
                        ),
                    ),
                )
            units.append(
                UnitProposal(
                    unit_id=unit.unit_id,
                    applicability="EXEMPT"
                    if outcome == "exempt"
                    else "UNCERTAIN"
                    if outcome == "uncertain"
                    else "APPLIES",
                    normative="MANDATORY",
                    lifecycle="CURRENT_OR_UNSPECIFIED",
                    conclusion="ISSUES"
                    if issues
                    else "NOT_APPLICABLE"
                    if outcome == "exempt"
                    else "INSUFFICIENT"
                    if outcome == "uncertain"
                    else "NO_CONFLICT",
                    explanation="Scripted source-based assessment",
                    support=support,
                    issues=issues,
                )
            )
        output = AssessmentBatchOutput(batch_id=request.batch_id, units=tuple(units))
        return ModelReply(
            output_json=output.model_dump_json(), input_tokens=1200, output_tokens=500
        )

    async def challenge(self, request, **kwargs):
        self.challenge_requests.append(request)
        decisions = []
        for item in request.candidates:
            unit = next(
                u for u in request.assessment.units if u.unit_id == item.unit_id
            )
            citations = refs(request.assessment, unit, "ADR_DOCUMENT") + refs(
                request.assessment, unit, "PR_HEAD"
            )
            if self.challenge_verdict == "REFUTED":
                alternate = [
                    e
                    for e in request.assessment.evidence
                    if e.kind == "CODE"
                    and e.origin == "PR_HEAD"
                    and e.path != unit.path
                    and e.excerpt
                ]
                citations += tuple(
                    QuoteRef(evidence_id=e.evidence_id, quote=e.excerpt[:1000])
                    for e in alternate[:1]
                )
            decisions.append(
                ChallengeDecision(
                    proposal_id=item.proposal_id,
                    verdict=self.challenge_verdict,
                    explanation="Independently tested binding rule, witness qualification and reachability, same-state revisions, exceptions and local impact against original evidence",
                    tested_witness=item.issue.core.witness,
                    witness_correspondence="UNCHANGED",
                    attribution=item.issue.attribution,
                    citations=citations,
                    revisions=item.issue.revisions,
                )
            )
        return ModelReply(
            output_json=ChallengeBatchOutput(
                batch_id=request.batch_id, decisions=tuple(decisions)
            ).model_dump_json(),
            input_tokens=1100,
            output_tokens=400,
        )


async def evaluate(ctx, source, model=None, **kwargs):
    return await DecisionIntegrityEvaluator(
        model or ScriptedModel(),
        source=source,
        authority=kwargs.pop("authority", TrustedAuthority()),
        token_counter=tokens,
        **kwargs,
    ).evaluate(ctx)


async def test_global_conventions_new_unmapped_exempt_and_preexisting():
    ctx, source = await fixture(
        files=[
            changed("api/new.py", None, AFTER, status="added"),
            changed("jobs/new.py", None, AFTER, status="added"),
            changed("generated/new.py", None, AFTER, status="added"),
        ]
    )
    model = ScriptedModel(
        {
            ("ADR", "api/new.py"): "conflict",
            ("ADR", "jobs/new.py"): "conflict",
            ("ADR", "generated/new.py"): "exempt",
        }
    )
    result = await evaluate(ctx, source, model)
    assert result.outcome == "CONFLICTS_FOUND"
    assert (
        len([f for f in result.findings if f.disposition == "SUPPORTED_CONFLICT"]) == 2
    )
    assert len(model.requests) == 1 and len(model.challenge_requests) == 1
    assert len(result.units) == 6 and any(u.status == "EXEMPT" for u in result.units)
    assert all(f.attribution == "INTRODUCED" for f in result.findings)


@pytest.mark.parametrize(
    "family,policy,before,after,channel",
    [
        (
            "semantic",
            "Order events must be published only after durable commit.",
            "def handler():\n    commit()\n    publish()\n",
            "def handler():\n    publish()\n    commit()\n",
            "ADR",
        ),
        (
            "rkg",
            None,
            "def handler(order_id):\n    return order_id\n",
            "def handler(order_id, tenant_id):\n    return order_id\n",
            "RKG",
        ),
    ],
)
async def test_semantic_and_rkg_positive_findings(
    family, policy, before, after, channel
):
    ctx, source = await fixture(
        policy=policy, files=[changed("service.py", before, after)]
    )
    model = ScriptedModel({(channel, "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert result.outcome == "CONFLICTS_FOUND"
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    assert result.findings[0].head_location.path == "service.py"
    assert result.input_tokens == 2300 and result.output_tokens == 900
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.findings[0].claim = "mutable"


async def test_alternate_safeguard_refutes_conflict():
    policy = "All order routes must authenticate requests; authentication may be delegated to middleware."
    before = "def handler():\n    authenticate()\n    return orders()\n"
    after = "@protected(authenticate)\ndef handler():\n    return orders()\n"
    guard = "def authenticate():\n    if not authenticated:\n        raise PermissionError('denied')\n\ndef protected(check):\n    def wrap(fn):\n        def guarded():\n            check()\n            return fn()\n        return guarded\n    return wrap\n"
    edge = relationship("e0", "guard", "auth.py", guard)
    ctx, source = await fixture(
        policy=policy,
        files=[changed("service.py", before, after)],
        edges=(edge,),
        extra_source={(BASE, "auth.py"): guard, (HEAD, "auth.py"): guard},
    )
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, challenge="REFUTED")
    result = await evaluate(ctx, source, model)
    assert result.findings and all(f.disposition == "REFUTED" for f in result.findings)
    assert any(
        p.method == "SEMANTIC_CHALLENGE" and not p.supported for p in result.proofs
    )
    assert any(
        e.path == "auth.py" and e.origin == "PR_HEAD"
        for e in model.requests[0].evidence
    )
    namespace = {"authenticated": False, "orders": lambda: ["order"]}
    exec(guard + after, namespace)  # noqa: S102 -- execute only the literal test fixture
    with pytest.raises(PermissionError):
        namespace["handler"]()
    namespace["authenticated"] = True
    assert namespace["handler"]() == ["order"]


def relationship(source_id, target_id, path, text):
    return {
        "source": entity(source_id, "handler" if source_id == "e0" else "consumer"),
        "target": entity(target_id, "handler" if target_id == "e0" else "guard"),
        "assertion": {
            "id": "observed-call",
            "repository": REPO,
            "relationshipType": "CALLS",
            "source_type": "CODE",
            "confidence": 0.9,
            "rationale": "Source-backed invocation",
        },
        "support": [
            {"chunk": chunk("support-call", path, text, BASE), "is_adr": False}
        ],
    }


@pytest.mark.parametrize("compatible", [False, True])
async def test_rkg_producer_consumer_pair(compatible):
    before = "def handler(order_id):\n    return order_id\n"
    after = (
        "def handler(order_id, tenant_id"
        + ("=None" if compatible else "")
        + "):\n    return order_id\n"
    )
    consumer = "from service import handler\ndef consumer():\n    return handler(42)\n"
    ctx, source = await fixture(
        policy=None,
        files=[changed("service.py", before, after)],
        edges=(relationship("consumer", "e0", "client.py", consumer),),
        extra_source={(BASE, "client.py"): consumer, (HEAD, "client.py"): consumer},
    )
    model = ScriptedModel(
        {("RKG", "service.py"): "clean" if compatible else "conflict"}
    )
    result = await evaluate(ctx, source, model)
    assert (
        ctx.relationships[0].source_id == "consumer"
        and ctx.relationships[0].target_id == "e0"
    )
    assert any(
        e.path == "client.py" and e.origin == "PR_HEAD"
        for e in model.requests[0].evidence
    )
    assert any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings) == (
        not compatible
    )
    namespace = {}
    exec(after, namespace)  # noqa: S102 -- execute only the literal test fixture
    if compatible:
        assert namespace["handler"](42) == 42
        assert not model.challenge_requests
    else:
        with pytest.raises(TypeError):
            namespace["handler"](42)


@pytest.mark.parametrize("outcome", ["clean", "impact", "uncertain"])
async def test_no_challenge_for_clean_advisory_or_uncertain(outcome):
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): outcome})
    result = await evaluate(ctx, source, model)
    assert not model.challenge_requests
    assert not any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    if outcome == "uncertain":
        assert result.outcome == "REVIEW_REQUIRED"
    if outcome == "clean":
        assert result.outcome == "NO_CONFLICTS_FOUND"


async def test_preexisting_not_new_conflict():
    ctx, source = await fixture()
    model = ScriptedModel(
        {("ADR", "service.py"): "conflict"}, attribution="PRE_EXISTING"
    )
    result = await evaluate(ctx, source, model)
    assert result.findings[0].disposition == "PRE_EXISTING"
    assert not model.challenge_requests


async def test_untrusted_scope_needs_original_adr_quotes():
    ctx, source = await fixture()

    class MetadataOnly(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            output = AssessmentBatchOutput.model_validate_json(reply.output_json)
            # Valid code quotations cannot substitute for original policy meaning.
            code_ids = {e.evidence_id for e in request.evidence if e.kind == "CODE"}
            output = output.model_copy(
                update={
                    "units": tuple(
                        u.model_copy(
                            update={
                                "support": tuple(
                                    q for q in u.support if q.evidence_id in code_ids
                                ),
                                "issues": tuple(
                                    i.model_copy(
                                        update={
                                            "rule": i.rule.model_copy(
                                                update={
                                                    "citations": tuple(
                                                        q
                                                        for q in i.core.citations
                                                        if q.evidence_id in code_ids
                                                    )
                                                }
                                            )
                                        }
                                    )
                                    for i in u.issues
                                ),
                            }
                        )
                        for u in output.units
                    )
                }
            )
            return reply.model_copy(update={"output_json": output.model_dump_json()})

    result = await evaluate(
        ctx, source, MetadataOnly({("ADR", "service.py"): "conflict"})
    )
    assert any("ORIGINAL_ADR_RULE_REQUIRED" in u.reason_codes for u in result.units)
    assert not result.findings


@pytest.mark.parametrize("authority", [None])
async def test_unavailable_authority_adapter_is_review(authority):
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, authority=authority)
    assert result.outcome == "REVIEW_REQUIRED"
    assert result.findings[0].disposition == "NEEDS_REVIEW"
    assert not model.challenge_requests


async def test_bad_citations_cannot_be_promoted():
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, invalid=True)
    result = await evaluate(ctx, source, model)
    assert result.coverage.uncertain == len(result.units) and not result.findings
    assert not model.challenge_requests


async def test_three_way_divergence_and_missing_target():
    ctx, source = await fixture(
        merge=MERGE, target="def handler():\n    return different()\n"
    )
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert any(d.target_comparison == "DIVERGED" for d in ctx.deltas)
    assert {e.commit_sha for e in model.requests[0].evidence if e.kind == "CODE"} >= {
        BASE,
        HEAD,
        MERGE,
    }

    # Deliberately remove target support from both assessment layers, preserving valid JSON.
    class MissingTarget(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            output = AssessmentBatchOutput.model_validate_json(reply.output_json)
            updates = []
            for u in output.units:
                issues = tuple(
                    i.model_copy(
                        update={
                            "revisions": tuple(
                                r.model_copy(
                                    update={"citations": (), "observation_ids": ()}
                                )
                                if r.revision == "TARGET_BASE"
                                else r
                                for r in i.revisions
                            )
                        }
                    )
                    for i in u.issues
                )
                updates.append(u.model_copy(update={"issues": issues}))
            return reply.model_copy(
                update={
                    "output_json": output.model_copy(
                        update={"units": tuple(updates)}
                    ).model_dump_json()
                }
            )

    result = await evaluate(
        ctx, source, MissingTarget({("ADR", "service.py"): "conflict"})
    )
    assert result.findings[0].attribution == "UNKNOWN"
    assert result.findings[0].disposition == "NEEDS_REVIEW"


async def test_model_failure_and_budget_accounting():
    ctx, source = await fixture()
    result = await evaluate(ctx, source, ScriptedModel(fail=True))
    assert result.outcome == "NOT_EVALUATED" and result.coverage.errors == 2
    assert len(result.calls) == 2 and result.reserved_unreported_tokens > 0
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(
        ctx, source, model, settings=EvaluationSettings(max_attempts=1)
    )
    assert len(result.calls) == 1 and result.findings[0].disposition == "NEEDS_REVIEW"
    result = await evaluate(ctx, source, settings=EvaluationSettings(max_tokens=1))
    assert not result.calls and result.coverage.omitted == 2


async def test_input_hash_revision_isolation_and_final_validation():
    ctx, source = await fixture()
    with pytest.raises(EvaluationInputError):
        await evaluate(ctx.model_copy(update={"context_hash": "0" * 64}), source)
    result = await evaluate(
        ctx, source, ScriptedModel({("ADR", "service.py"): "conflict"})
    )
    data = result.model_dump(mode="json")
    data["findings"][0]["attribution"] = "PRE_EXISTING"
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate_json(json.dumps(data))


async def test_quote_resolution_unicode_ambiguity_and_forbidden_offsets():
    ctx, _ = await fixture(policy="Unicode cafÃƒÆ’Ã‚Â©. Unicode cafÃƒÆ’Ã‚Â©.")
    e = next(e for e in ctx.evidence if e.kind == "ADR")
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        resolve_quote(
            QuoteRef(evidence_id=e.evidence_id, quote="cafÃƒÆ’Ã‚Â©"), {e.evidence_id: e}
        )
    c = resolve_quote(
        QuoteRef(evidence_id=e.evidence_id, quote=e.excerpt), {e.evidence_id: e}
    )
    assert c.end_char == len(e.excerpt) and c.location is None
    with pytest.raises(ValidationError):
        QuoteRef(evidence_id=e.evidence_id, quote="cafÃƒÆ’Ã‚Â©", start_char=0)


@pytest.mark.parametrize("source_id", [None, "legacy-source", "owner-upload-v1:other"])
async def test_repository_adr_authority_without_upload_marker(tmp_path, source_id):
    ctx, source = await fixture()
    path = tmp_path / REPO / "adr-1" / "original.md"
    path.parent.mkdir(parents=True)
    path.write_text(POLICY, encoding="utf-8")
    record = SimpleNamespace(
        id="adr-1",
        repository_id=REPO,
        content_hash=digest(POLICY),
        source_type="MANUAL_UPLOAD",
        source_id=source_id,
        status="COMPLETED",
        file_path=str(path),
    )
    store = SimpleNamespace(
        get_repository=lambda _: SimpleNamespace(id=REPO, user_id="owner"),
        get_adr=lambda _: record,
    )
    authority = UploadAuthority(store, tmp_path)
    assert authority.receipts(ctx)[0].status == "APPROVED"
    assert authority.receipts(ctx)[0].policy_version == "repository-adr-v1"
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model, authority=authority)
    assert result.outcome == "CONFLICTS_FOUND"
    assert any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    assert len(model.challenge_requests) == 1
    assert record.source_id == source_id  # Read-only: existing metadata is preserved.
    record.source_type = "LEGACY"
    assert authority.receipts(ctx)[0].status == "APPROVED"
    for field, invalid in (
        ("repository_id", "another-repository"),
        ("id", "another-document"),
        ("content_hash", digest("another-document")),
        ("status", "PENDING"),
        ("status", "PROCESSING"),
        ("status", "FAILED"),
        ("file_path", str(tmp_path / "outside.md")),
        ("file_path", str(path.parent / "missing.md")),
    ):
        original = getattr(record, field)
        setattr(record, field, invalid)
        assert authority.receipts(ctx)[0].status == "UNKNOWN", (field, invalid)
        setattr(record, field, original)
    store.get_repository = lambda _: SimpleNamespace(
        id="another-repository", user_id="owner"
    )
    assert authority.receipts(ctx)[0].status == "UNKNOWN"
    store.get_repository = lambda _: None
    assert authority.receipts(ctx)[0].status == "UNKNOWN"
    store.get_repository = lambda _: SimpleNamespace(id=REPO, user_id="owner")
    store.get_adr = lambda _: None
    assert authority.receipts(ctx)[0].status == "UNKNOWN"
    store.get_adr = lambda _: record
    path.write_text("changed", encoding="utf-8")
    assert authority.receipts(ctx)[0].reason == "UPLOAD_HASH_MISMATCH"


def test_profiles_and_cost_requirements():
    assert EvaluationSettings().max_attempts == 8
    assert EvaluationSettings.expanded().max_tokens == 240000
    with pytest.raises(ValidationError):
        EvaluationSettings(max_cost=1.0)


@pytest.mark.parametrize(
    "name", ["stage_4_1_example_context.json", "4_1_scope_enriched_context.json"]
)
def test_historical_context_hash_compatibility(name):
    path = Path(__file__).resolve().parents[3] / "instructions" / name
    if not path.exists():
        pytest.skip("Optional local historical context artifact is not tracked")
    ctx = ArchitectureContext.model_validate_json(path.read_text(encoding="utf-8"))
    assert validate_context(ctx) == ctx


async def test_worsening_challenge_insufficient_and_missing_proof():
    ctx, source = await fixture()
    model = ScriptedModel({("ADR", "service.py"): "conflict"}, attribution="WORSENED")
    result = await evaluate(ctx, source, model)
    assert result.findings[0].disposition == "SUPPORTED_CONFLICT"
    result = await evaluate(
        ctx,
        source,
        ScriptedModel({("ADR", "service.py"): "conflict"}, challenge="INSUFFICIENT"),
    )
    assert result.findings[0].disposition == "NEEDS_REVIEW"

    class MissingProof(ScriptedModel):
        async def assess(self, request, **kwargs):
            from ai_services.decision_guard.evaluation.models import MissingFact

            reply = await super().assess(request, **kwargs)
            output = AssessmentBatchOutput.model_validate_json(reply.output_json)
            units = tuple(
                u.model_copy(
                    update={
                        "issues": tuple(
                            i.model_copy(
                                update={
                                    "missing_facts": (
                                        MissingFact(
                                            fact_id="causality",
                                            scope="CORE",
                                            fact="Whether the subject reaches the boundary",
                                            relevance="Required for this local allegation",
                                            unrequestable_reason="No source anchor supplied",
                                        ),
                                    )
                                }
                            )
                            for i in u.issues
                        )
                    }
                )
                for u in output.units
            )
            return reply.model_copy(
                update={
                    "output_json": output.model_copy(
                        update={"units": units}
                    ).model_dump_json()
                }
            )

    model = MissingProof({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert (
        not model.challenge_requests
        and result.findings[0].disposition == "NEEDS_REVIEW"
    )


async def test_timeout_cancel_and_monetary_cap():
    ctx, source = await fixture()

    class Slow(ScriptedModel):
        async def assess(self, request, **kwargs):
            await asyncio.sleep(1)
            return await super().assess(request, **kwargs)

    result = await evaluate(
        ctx,
        source,
        Slow(),
        settings=EvaluationSettings(call_timeout_seconds=0.01, retries=0),
    )
    assert result.calls[0].status == "MODEL_TIMEOUT" and result.coverage.errors == 2
    task = asyncio.create_task(evaluate(ctx, source, Slow()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    settings = EvaluationSettings(
        input_rate_per_million=1.0,
        output_rate_per_million=2.0,
        pricing_revision="fixture-rates",
        currency="USD",
        max_cost=0.000001,
    )
    result = await evaluate(ctx, source, settings=settings)
    assert not result.calls and result.estimated_cost == 0


async def test_response_manifest_and_schema_rejection():
    ctx, source = await fixture()

    class BadManifest(ScriptedModel):
        async def assess(self, request, **kwargs):
            return ModelReply(
                output_json='{"batch_id":"foreign", "units":[]}',
                input_tokens=10,
                output_tokens=10,
            )

    result = await evaluate(ctx, source, BadManifest())
    assert result.coverage.errors == 2 and len(result.calls) == 2
    assert all(c.status == "INVALID_OUTPUT" for c in result.calls)


async def test_cross_repository_supplement_rejected():
    ctx, source = await fixture()
    reader = source.read_blob
    source.read_blob = lambda *args: reader(*args).model_copy(
        update={"repository_id": "foreign"}
    )
    with pytest.raises(EvaluationInputError, match="identity"):
        await evaluate(ctx, source)


async def test_inactive_and_unknown_normative_intent_cannot_confirm():
    ctx, source = await fixture(
        policy="Status: Proposed. All new Python variables must use camelCase."
    )

    class Proposed(ScriptedModel):
        async def assess(self, request, **kwargs):
            reply = await super().assess(request, **kwargs)
            output = AssessmentBatchOutput.model_validate_json(reply.output_json)
            units = tuple(
                u.model_copy(update={"lifecycle": "PROPOSED"})
                if any(
                    x.unit_id == u.unit_id and x.channel == "ADR" for x in request.units
                )
                else u
                for u in output.units
            )
            return reply.model_copy(
                update={
                    "output_json": output.model_copy(
                        update={"units": units}
                    ).model_dump_json()
                }
            )

    model = Proposed({("ADR", "service.py"): "conflict"})
    result = await evaluate(ctx, source, model)
    assert (
        not model.challenge_requests
        and result.findings[0].disposition == "NEEDS_REVIEW"
    )


async def test_harness_dry_run_never_builds_client(capsys, tmp_path):
    from ai_services.runs.run_decision_guard_evaluation import run

    ctx, _ = await fixture()
    path = tmp_path / "context.json"
    path.write_text(ctx.model_dump_json(), encoding="utf-8")
    args = SimpleNamespace(
        context=path,
        max_attempts=2,
        max_tokens=60000,
        deadline=120.0,
        max_input_tokens=16000,
        model_window=32768,
        live=False,
    )
    await run(args)
    output = json.loads(capsys.readouterr().out)
    assert output["model_calls"] == 0 and output["units"] == 2


async def test_ledger_over_128_units_expanded_and_bounded_concurrency():
    ctx, source = await fixture(
        files=[changed(f"src/f{i}.py", BEFORE, AFTER) for i in range(65)],
        context_settings=ContextSettings(
            max_context_tokens=250000, max_entities=150, max_chunks=200
        ),
    )

    class Concurrent(ScriptedModel):
        active = peak = 0

        async def assess(self, request, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.005)
                return await super().assess(request, **kwargs)
            finally:
                self.active -= 1

    model = Concurrent()
    result = await evaluate(
        ctx, source, model, settings=EvaluationSettings.expanded(max_reads=300)
    )
    assert len(result.units) == 130
    assert result.coverage.omitted == 0 and result.coverage.errors == 0
    assert model.peak <= 3 and len(result.calls) <= 24
    assert not model.challenge_requests


async def test_azure_adapter_disables_hidden_retries_and_uses_structured_schema():
    captured = {}

    class Client:
        def with_options(self, **kwargs):
            captured.update(kwargs)
            return self

        @property
        def beta(self):
            return self

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        async def parse(self, **kwargs):
            captured.update(kwargs)
            from ai_services.decision_guard.evaluation.wire import WireAssessment

            output = WireAssessment(batch_id="b", quotes=(), units=())
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=40, completion_tokens=20),
                id="r",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(parsed=output, refusal=None),
                        finish_reason="stop",
                    )
                ],
            )

    model = AzureEvaluationModel(
        SimpleNamespace(client=Client(), deployment="deployment"),
        deployment_revision="revision",
    )
    request = SimpleNamespace(
        model_dump_json=lambda **kwargs: "{}",
        model_dump=lambda **kwargs: {
            "revision": {"repository_id": REPO},
            "units": [],
            "evidence": [],
            "context_records_json": json.dumps(
                {"deltas": [], "entities": [], "relationships": [], "requirements": []}
            ),
            "limitations": [],
            "adr_retrieval": "ABSENT",
            "adr_catalog_complete": True,
        },
        limitations=(),
        evidence=(),
    )
    reply = await model.assess(request, max_output_tokens=100, timeout=1.0)
    assert captured["max_retries"] == 0 and captured["max_completion_tokens"] == 100
    from ai_services.decision_guard.evaluation.wire import WireAssessment

    assert (
        captured["response_format"] is WireAssessment and "temperature" not in captured
    )
    assert reply.input_tokens == 40 and reply.output_tokens == 20
