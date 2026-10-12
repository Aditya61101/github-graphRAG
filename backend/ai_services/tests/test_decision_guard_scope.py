"""Integration of the persisted enrichment contract with Phase 4.1 discovery."""

import asyncio

import pytest
from pydantic import ValidationError

from ai_services.decision_guard import (
    ArchitectureContextBuilder,
    ContextSettings,
    ModelTokenCounter,
)
from ai_services.decision_guard.context import RepositoryScopeError
from ai_services.decision_guard.graph import COMMUNITIES, REQUIREMENTS, SCOPE_EVIDENCE
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.ingestion.adr.applicability import (
    Applicability,
    ApplicabilityCondition,
    ApplicabilityMetadata,
    ApplicabilityTarget,
    applicability_properties,
)
from ai_services.tests import test_decision_guard_context as base_context
from ai_services.tests.test_decision_guard_context import (
    REPO,
    Graph,
    Readiness,
    Source,
    adr_support,
    changed,
    changes,
    entity,
    requirement,
)


@pytest.fixture
def context_setup():
    return base_context.setup.__wrapped__()


def scope_support(text="All application variables must follow camelCase."):
    support = adr_support()
    support["chunk"] = {
        "id": "adr:adr-1:chunk:3",
        "repository": REPO,
        "adr_id": "adr-1",
        "text": text,
        "contentHash": digest(text),
        "section": "Applicability",
    }
    return support


def enriched(
    meta,
    kid="scope-rule",
    *,
    statement="All application variables use camelCase",
    targets=(),
):
    row = requirement(kid, targets=list(targets), statement=statement)
    row["knowledge"]["title"] = statement
    row["knowledge"].update(applicability_properties(meta, "fixture-model-v2"))
    row["knowledge"]["applicability_enriched_at"] = 12345
    return row


def repository_metadata():
    return ApplicabilityMetadata(
        applicability=Applicability(
            scopes=["repository"],
            basis="explicit",
            rationale="Applies to identifiers throughout the application.",
            evidence_chunk_ids=["adr:adr-1:chunk:3"],
        )
    )


def community_metadata(*, governs=True, basis="explicit", scope="community"):
    return ApplicabilityMetadata(
        applicability=Applicability(
            scopes=[scope],
            basis=basis,
            rationale="The subsystem itself is governed.",
            targets=[
                ApplicabilityTarget(
                    kind="community" if scope == "community" else "entity",
                    name="Request Processing"
                    if scope == "community"
                    else "process_request",
                    label=None if scope == "community" else "Module",
                    basis=basis,
                    governs_entire_scope=governs,
                    evidence_chunk_ids=["adr:adr-1:chunk:3"],
                )
            ],
            evidence_chunk_ids=["adr:adr-1:chunk:3"],
        ),
        community_ids=["comm-requests"],
    )


def attach(graph, rows):
    graph.documents = [{"adr": adr_support()["adr"]}]
    graph.knowledge = rows
    graph.scope_support = [scope_support()]


def attach_community(graph, *, repo=REPO):
    graph.community_rows = [
        {
            "entity": entity(),
            "community": {
                "communityId": "comm-requests",
                "repository": repo,
                "name": "Request Processing",
                "architecturalRole": "request processing",
                "aliases": [],
            },
        }
    ]


async def test_new_snake_case_file_discovers_repository_camel_case_rule(context_setup):
    graph, _, _, builder, _ = context_setup
    graph.chunks = []
    meta = repository_metadata()
    attach(graph, [enriched(meta)])
    ctx = await builder().build(
        changes(
            changed("src/new_module.py", None, "invoice_total = 0\n", status="added")
        )
    )
    assert not ctx.entities
    rule = ctx.requirements[0]
    assert "REPOSITORY_SCOPE" in rule.discovery_reasons
    assert rule.declared_applicability == meta
    assert rule.applicability == "UNASSESSED"
    assert rule.scope_evidence_ids
    assert any(
        e.chunk_id == "adr:adr-1:chunk:3" and e.section == "Applicability"
        for e in ctx.evidence
    )


async def test_justified_community_scope_discovery(context_setup):
    graph, _, _, builder, _ = context_setup
    meta = community_metadata()
    attach(graph, [enriched(meta, statement="Subsystem communicates asynchronously")])
    attach_community(graph)
    ctx = await builder().build(changes())
    rule = ctx.requirements[0]
    assert rule.supported_community_ids == ("comm-requests",)
    assert "COMMUNITY_SCOPE" in rule.discovery_reasons
    assert rule.declared_applicability.community_ids == ["comm-requests"]


async def test_existing_entity_relationships_still_discover_requirements(context_setup):
    graph, _, _, builder, _ = context_setup
    attach(graph, [enriched(ApplicabilityMetadata(), targets=[entity()])])
    rule = (await builder().build(changes())).requirements[0]
    assert "ENTITY_TARGET" in rule.discovery_reasons
    assert rule.target_links[0].relationship == "AFFECTS"


@pytest.mark.parametrize(
    "metadata",
    [
        community_metadata(governs=False),
        community_metadata(basis="uncertain"),
        community_metadata(scope="entity"),
    ],
)
async def test_member_or_unsupported_scope_never_promotes_community(
    metadata, context_setup
):
    graph, _, _, builder, _ = context_setup
    attach(graph, [enriched(metadata, targets=[entity()])])
    attach_community(graph)
    rule = (await builder().build(changes())).requirements[0]
    assert not rule.supported_community_ids
    assert "COMMUNITY_SCOPE" not in rule.discovery_reasons
    assert "ENTITY_TARGET" in rule.discovery_reasons
    assert rule.declared_applicability == metadata


async def test_conditions_exceptions_qualifiers_and_versions_survive(context_setup):
    graph, _, _, builder, _ = context_setup
    meta = repository_metadata()
    meta.applicability.conditions = [
        ApplicabilityCondition(
            kind="language", expression="For Python only", selectors=["python"]
        ),
        ApplicabilityCondition(
            kind="directory", expression="Under src/jobs/", selectors=["src/jobs/"]
        ),
    ]
    meta.applicability.exceptions = ["Generated vendor files are exempt."]
    meta.applicability.qualifiers = ["Identifiers owned by the application."]
    meta.graph_snapshot = "graph-association-version-7"
    attach(graph, [enriched(meta)])
    ctx = await builder().build(
        changes(changed("src/jobs/new_job.py", None, "job_count = 1\n", status="added"))
    )
    rule = ctx.requirements[0]
    assert rule.declared_applicability.to_json() == meta.to_json()
    assert "CONDITIONAL_SCOPE" in rule.discovery_reasons
    assert rule.enrichment_model == "fixture-model-v2"
    assert rule.scope_metadata_status == "VALID"
    assert rule.applicability == "UNASSESSED"
    assert ArchitectureContext.model_validate_json(ctx.model_dump_json()) == ctx
    # Defensive copies keep mutable enrichment lists out of the frozen context.
    rule.declared_applicability.applicability.exceptions.append("mutated externally")
    assert (
        rule.declared_applicability.applicability.exceptions
        == meta.applicability.exceptions
    )
    with pytest.raises(ValidationError):
        rule.declared_applicability_json = "{}"


async def test_scope_and_version_changes_invalidate_identity_without_adr_content_change(
    context_setup,
):
    graph, _, _, builder, _ = context_setup
    row = enriched(repository_metadata())
    attach(graph, [row])
    before = await builder().build(changes())
    meta = repository_metadata()
    meta.applicability.exceptions = ["Migration scripts may use snake_case."]
    row["knowledge"].update(applicability_properties(meta, "fixture-model-v3"))
    after = await builder().build(changes())
    assert before.baseline == after.baseline
    assert before.adr_snapshot[0].content_hash == after.adr_snapshot[0].content_hash
    assert (
        before.adr_snapshot[0].applicability_fingerprint
        != after.adr_snapshot[0].applicability_fingerprint
    )
    assert before.adr_catalog_hash != after.adr_catalog_hash
    assert before.context_hash != after.context_hash
    assert (
        before.requirements[0].enrichment_fingerprint
        != after.requirements[0].enrichment_fingerprint
    )


async def test_unselected_enrichment_changes_invalidate_catalog_identity(context_setup):
    graph, _, _, builder, _ = context_setup
    row = enriched(
        ApplicabilityMetadata(), kid="low-ranked", statement="Shipping carrier policy"
    )
    attach(graph, [enriched(repository_metadata()), row])
    build = builder(ContextSettings(max_decisions=1))
    before = await build.build(changes())
    row["knowledge"]["applicability_model"] = "new-model-revision"
    after = await build.build(changes())
    assert before.requirements == after.requirements
    assert before.adr_catalog_hash != after.adr_catalog_hash
    assert before.context_hash != after.context_hash


async def test_unknown_legacy_invalid_and_scope_mismatches_remain_fallback(
    context_setup,
):
    graph, _, _, builder, _ = context_setup
    uncertain = ApplicabilityMetadata(
        applicability=Applicability(scopes=["entity"], basis="inferred"),
        entity_ids=["unmatched"],
    )
    inferred = enriched(uncertain, kid="uncertain", statement="Retry conservatively")
    legacy = requirement("legacy", statement="Protect opaque invariants", targets=[])
    legacy["knowledge"]["title"] = "Opaque policy"
    invalid = enriched(
        ApplicabilityMetadata(), kid="invalid", statement="Opaque future policy"
    )
    invalid["knowledge"]["applicability_json"] = '{"schema_version":"future-v2"}'
    mismatch = enriched(
        ApplicabilityMetadata(
            applicability=Applicability(scopes=["entity"], basis="explicit"),
            entity_ids=["absent"],
        ),
        kid="mismatch",
        statement="Opaque explicit policy",
    )
    attach(graph, [inferred, legacy, invalid, mismatch])
    ctx = await builder().build(changes())
    by_id = {r.requirement_id: r for r in ctx.requirements}
    assert set(by_id) == {"uncertain", "legacy", "invalid", "mismatch"}
    assert by_id["uncertain"].discovery_reasons == ("UNCERTAIN_SCOPE_FALLBACK",)
    assert by_id["legacy"].scope_metadata_status == "LEGACY"
    assert by_id["invalid"].scope_metadata_status == "INVALID"
    assert by_id["mismatch"].discovery_reasons == ("SCOPE_MISMATCH_FALLBACK",)
    assert all(r.applicability == "UNASSESSED" for r in by_id.values())


@pytest.mark.parametrize("channel", ["community", "scope_evidence"])
async def test_scope_queries_reject_cross_repository_data(context_setup, channel):
    graph, _, _, builder, _ = context_setup
    attach(graph, [enriched(community_metadata())])
    attach_community(graph, repo="foreign" if channel == "community" else REPO)
    if channel == "scope_evidence":
        graph.scope_support[0]["chunk"]["repository"] = "foreign"
    with pytest.raises(RepositoryScopeError):
        await builder().build(changes())


async def test_missing_scope_evidence_does_not_establish_supported_community(
    context_setup,
):
    graph, _, _, builder, _ = context_setup
    attach(graph, [enriched(community_metadata())])
    attach_community(graph)
    graph.scope_support = []
    ctx = await builder().build(changes())
    assert not ctx.requirements[0].supported_community_ids
    assert "COMMUNITY_SCOPE" not in ctx.requirements[0].discovery_reasons
    assert "SCOPE_EVIDENCE_MISSING" in {l.code for l in ctx.coverage.limitations}


async def test_successful_complete_patch_is_not_a_limitation(context_setup):
    _, _, _, builder, _ = context_setup
    file = changed(
        patch='@@ -1,2 +1,2 @@\n def process_request():\n-    return "old"\n+    return "new"\n'
    )
    ctx = await builder().build(changes(file))
    assert not ctx.coverage.files[0].reasons
    assert not ctx.coverage.limitations


async def test_model_counter_retains_more_evidence_without_raising_limit(context_setup):
    graph, source, readiness, _, locks = context_setup
    attach(graph, [enriched(repository_metadata())])
    lock = locks.setdefault("owner/repo", asyncio.Lock())
    settings = ContextSettings(max_context_tokens=6000)
    byte_ctx = await ArchitectureContextBuilder(
        graph, source, readiness, lambda _: lock, settings
    ).build(changes())
    counter = ModelTokenCounter(
        "model:fixture", lambda text: (len(text.encode("utf-8")) + 3) // 4
    )
    model_ctx = await ArchitectureContextBuilder(
        graph, source, readiness, lambda _: lock, settings, counter
    ).build(changes())
    assert len(model_ctx.requirements) > len(byte_ctx.requirements)
    assert model_ctx.budget.token_limit == byte_ctx.budget.token_limit == 6000
    assert model_ctx.budget.estimator == "model:fixture"
    assert (
        counter(model_ctx.model_dump_json())
        <= model_ctx.budget.estimated_tokens
        <= 6000
    )


def test_invalid_tokenizer_count_is_rejected():
    with pytest.raises(ValueError):
        ModelTokenCounter("bad", lambda _: -1)("context")
    with pytest.raises(ValueError):
        ModelTokenCounter("bad", lambda _: True)("context")


def test_scope_queries_preserve_read_scope_and_hints():
    assert "properties(k) AS knowledge" in REQUIREMENTS
    for prop in (
        "applicability_entity_ids",
        "applicability_community_ids",
        "applicability_repository_wide",
        "applicability_languages",
    ):
        assert prop in REQUIREMENTS
    assert "MEMBER_OF" in COMMUNITIES and "repository: $repo" in COMMUNITIES
    assert "ch.adr_id = adr.id" in SCOPE_EVIDENCE
    assert "adr.id IN $adr_ids" in SCOPE_EVIDENCE


async def representative_scope_context():
    """Fixture generation shared with the checked-in example (no live clients)."""
    graph, source, readiness = Graph(), Source(), Readiness()
    meta = repository_metadata()
    meta.applicability.conditions = [
        ApplicabilityCondition(
            kind="language",
            expression="For application Python identifiers",
            selectors=["python"],
        )
    ]
    meta.applicability.exceptions = ["Generated bindings are exempt."]
    attach(
        graph,
        [
            enriched(meta),
            enriched(
                community_metadata(),
                kid="subsystem-rule",
                statement="Request subsystem communicates asynchronously",
                targets=[entity()],
            ),
        ],
    )
    attach_community(graph)
    lock = asyncio.Lock()
    return await ArchitectureContextBuilder(
        graph,
        source,
        readiness,
        lambda _: lock,
        ContextSettings(),
        ModelTokenCounter(
            "fixture:utf8-quarter", lambda text: (len(text.encode("utf-8")) + 3) // 4
        ),
    ).build(changes())


async def test_representative_scope_fixture_is_deterministic():
    before, after = (
        await representative_scope_context(),
        await representative_scope_context(),
    )
    assert before == after
    assert {r.requirement_id for r in before.requirements} == {
        "scope-rule",
        "subsystem-rule",
    }
    assert all(
        r.scope_metadata_status == "VALID" and r.scope_evidence_ids
        for r in before.requirements
    )
    assert before.adr_snapshot[0].applicability_fingerprint
