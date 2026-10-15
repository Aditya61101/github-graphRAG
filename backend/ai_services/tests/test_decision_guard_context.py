from __future__ import annotations

import asyncio
import subprocess
import threading
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from ai_services.decision_guard.context import (
    ArchitectureContextBuilder,
    ContextConstructionError,
    RepositoryScopeError,
)
from ai_services.decision_guard.delta import parse_hunks
from ai_services.decision_guard.graph import Neo4jArchitectureGraphReader
from ai_services.decision_guard.models import (
    ArchitectureContext,
    BaselineSnapshot,
    digest,
)
from ai_services.decision_guard.ports import Page, SourceBlob
from ai_services.decision_guard.settings import ContextSettings
from ai_services.decision_guard.source import GitRevisionSourceReader
from ai_services.ingestion.pr.git_reader import PullRequestGitReader
from ai_services.ingestion.pr.models import ChangedFile, PullRequestChangeSet
from ai_services.ingestion.pr.settings import PullRequestSettings
from ai_services.ingestion.sources.github import GitHubRepositorySource

BASE, HEAD, MERGE = "a" * 40, "b" * 40, "c" * 40
REPO = "repo_101"
OLD = 'def process_request():\n    return "old"\n'
NEW = 'def process_request():\n    return "new"\n'


def changed(path="service.py", before=OLD, after=NEW, **overrides):
    values = {
        "path": path,
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": None,
        "patch_status": "complete",
        "base_content": before,
        "head_content": after,
        "base_content_status": "complete" if before is not None else "absent",
        "head_content_status": "complete" if after is not None else "absent",
    }
    values.update(overrides)
    return ChangedFile(**values)


def changes(*files, **overrides):
    values = {
        "installation_id": 10,
        "github_repository_id": 101,
        "repository_full_name": "owner/repo",
        "repository_private": False,
        "pull_request_number": 42,
        "title": "Architectural change",
        "body": None,
        "base_branch": "main",
        "head_branch": "feature",
        "base_sha": BASE,
        "head_sha": HEAD,
        "base_repository_id": 101,
        "source_event": "opened",
        "repository_id": REPO,
        "merge_base_sha": BASE,
        "files": list(files) or [changed()],
    }
    values.update(overrides)
    return PullRequestChangeSet(**values)


def entity(eid="process", name="process_request", *, repo=REPO, source=None):
    return {
        "id": eid,
        "name": name,
        "label": "Module",
        "repository": repo,
        "source": source,
        "aliases": [],
    }


def chunk(cid="chunk-service", path="service.py", text=OLD, commit=BASE, *, repo=REPO):
    return {
        "id": cid,
        "repository": repo,
        "filePath": path,
        "text": text,
        "commit": commit,
        "contentHash": digest(text),
    }


def code_row():
    return {"path": "service.py", "chunk": chunk(), "entities": [entity()]}


def adr_support():
    text = "The process_request workflow must preserve request status transitions."
    return {
        "adr": {
            "id": "adr-1",
            "repository": REPO,
            "content_hash": digest("full ADR document"),
            "source_name": "workflow.md",
            "source_type": "MANUAL_UPLOAD",
            "source_version": "1",
        },
        "chunk": {
            "id": "adr:adr-1:chunk:2",
            "repository": REPO,
            "adr_id": "adr-1",
            "text": text,
            "contentHash": digest(text),
            "section": "Decision",
        },
    }


def requirement(kid="decision-1", kind="DECISION", targets=None, statement=None):
    return {
        "knowledge": {
            "id": kid,
            "repository": REPO,
            "title": "Request workflow" if kind == "DECISION" else None,
            "description": statement
            or "process_request must preserve request status transitions.",
            "decision_type": "workflow" if kind == "DECISION" else None,
            "constraint_type": "workflow" if kind == "CONSTRAINT" else None,
        },
        "kind": kind,
        "targets": [
            {
                **target,
                "relationship": "AFFECTS" if kind == "DECISION" else "CONSTRAINS",
            }
            for target in (targets if targets is not None else [entity()])
        ],
        "support": [adr_support()],
    }


class Source:
    def __init__(self):
        self.values = {(BASE, "service.py"): OLD, (HEAD, "service.py"): NEW}
        self.calls = []

    def read_blob(self, repository_id, sha, path):
        self.calls.append((repository_id, sha, path))
        value = self.values.get((sha, path))
        return SourceBlob(
            repository_id=repository_id,
            commit_sha=sha,
            path=path,
            status="complete" if value is not None else "absent",
            text=value,
        )


class Readiness:
    def __init__(self):
        self.state = BaselineSnapshot(
            repository_id=REPO,
            github_repository_id="101",
            tracked_branch="main",
            indexed_rkg_sha=BASE,
            repository_status="COMPLETED",
            readiness="READY",
            graph_generation=7,
            ingestion_run_id="run-1",
            unfinished_ingestion=False,
        )

    async def snapshot(self, repository_id):
        return self.state


class Graph:
    def __init__(self):
        self.chunks = [code_row()]
        self.edges = []
        self.documents = []
        self.knowledge = []
        self.community_rows = []
        self.scope_support = []
        self.fail_adr = False
        self.fail_requirements = False
        self.calls = []

    @contextmanager
    def read_snapshot(self, repository_id):
        self.calls.append(("snapshot", repository_id))
        yield self

    @staticmethod
    def page(rows, limit):
        return Page(tuple(rows[:limit]), len(rows) > limit)

    def file_chunks(self, paths, limit):
        self.calls.append(("paths", paths))
        return self.page([r for r in self.chunks if r["path"] in paths], limit)

    def entity_chunks(self, ids, limit):
        self.calls.append(("entity_sources", ids))
        return self.page(
            [
                {**r, "entities": [e for e in r["entities"] if e["id"] in ids]}
                for r in self.chunks
                if any(e["id"] in ids for e in r["entities"])
            ],
            limit,
        )

    def relationships(self, ids, limit):
        self.calls.append(("edges", ids))
        return self.page(
            [
                r
                for r in self.edges
                if r["source"]["id"] in ids or r["target"]["id"] in ids
            ],
            limit,
        )

    def adr_catalog(self, limit):
        if self.fail_adr:
            raise RuntimeError("offline")
        return self.page(self.documents, limit)

    def communities(self, ids, limit):
        return self.page(
            [row for row in self.community_rows if row["entity"]["id"] in ids], limit
        )

    def applicability_evidence(self, adr_ids, chunk_ids, limit):
        support = self.scope_support + [
            s for row in self.knowledge for s in row["support"]
        ]
        return self.page(
            [
                s
                for s in support
                if s.get("adr")
                and s.get("chunk")
                and s["adr"]["id"] in adr_ids
                and s["chunk"]["id"] in chunk_ids
            ],
            limit,
        )

    def requirements(
        self, ids, limit, *, fallback=False, community_ids=(), languages=()
    ):
        if self.fail_requirements:
            raise RuntimeError("offline")
        rows = []
        for row in self.knowledge:
            n = row["knowledge"]
            if (
                fallback
                or any(t["id"] in ids for t in row["targets"])
                or n.get("applicability_repository_wide")
                or set(n.get("applicability_entity_ids", ())) & set(ids)
                or set(n.get("applicability_community_ids", ())) & set(community_ids)
                or set(n.get("applicability_languages", ())) & set(languages)
            ):
                rows.append({**row, "adr_mention": row.get("adr_mention", False)})
        return self.page(rows, limit)


@pytest.fixture
def setup():
    graph, source, readiness = Graph(), Source(), Readiness()
    locks = {}

    def lock(key):
        return locks.setdefault(key, asyncio.Lock())

    def builder(settings=None):
        return ArchitectureContextBuilder(graph, source, readiness, lock, settings)

    return graph, source, readiness, builder, locks


async def test_representative_context_is_deterministic_and_immutable(setup):
    graph, source, _, builder, locks = setup
    support = adr_support()
    graph.documents = [{"adr": support["adr"]}]
    graph.knowledge = [requirement(), requirement("constraint-1", "CONSTRAINT")]
    result = await builder().build(changes())
    repeated = await builder().build(changes())
    assert result == repeated
    assert result.coverage_mode == "RKG_ADR"
    assert result.baseline.graph_generation == 7
    assert result.entities[0].mapping == "CHANGED_SYMBOL"
    assert result.deltas[0].symbols == ("process_request",)
    assert result.deltas[0].head_location.start_line == 2
    assert {e.origin for e in result.evidence} >= {
        "MERGE_BASE",
        "TARGET_BASE",
        "PR_HEAD",
        "INDEXED_CODE",
        "ADR_DOCUMENT",
    }
    assert {r.kind for r in result.requirements} == {"DECISION", "CONSTRAINT"}
    assert {r.target_links[0].relationship for r in result.requirements} == {
        "AFFECTS",
        "CONSTRAINS",
    }
    adr = next(e for e in result.evidence if e.kind == "ADR")
    assert adr.document_hash == support["adr"]["content_hash"]
    assert adr.excerpt == support["chunk"]["text"]
    assert adr.source_name == "workflow.md"
    assert result.budget.estimated_tokens <= result.budget.token_limit
    assert not locks["owner/repo"].locked()
    assert all(call[0] == REPO for call in source.calls)
    assert ArchitectureContext.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.context_hash = "changed"
    with pytest.raises(ValidationError):
        result.entities[0].name = "changed"
    with pytest.raises(ValidationError):
        ArchitectureContext.model_validate({**result.model_dump(), "unknown": True})


async def test_mention_does_not_claim_every_entity_changed(setup):
    graph, _, _, builder, _ = setup
    graph.chunks[0]["entities"] = [entity(), entity("lib", "External Library")]
    ctx = await builder().build(changes())
    by_id = {e.entity_id: e for e in ctx.entities}
    assert by_id["process"].mapping == "CHANGED_SYMBOL"
    assert by_id["lib"].mapping == "AMBIGUOUS"


async def test_repeated_chunk_text_preserves_ambiguity(setup):
    _, source, _, builder, _ = setup
    source.values[BASE, "service.py"] = OLD + OLD
    ctx = await builder().build(changes())
    assert ctx.entities[0].mapping == "AMBIGUOUS"
    assert any(e.placement == "AMBIGUOUS" for e in ctx.evidence)
    assert "CHUNK_AMBIGUOUS" in {l.code for l in ctx.coverage.limitations}


async def test_same_symbol_alias_collision_is_ambiguous(setup):
    graph, _, _, builder, _ = setup
    graph.chunks[0]["entities"] = [entity(), entity("same-name", "process_request")]
    ctx = await builder().build(changes())
    assert all(e.mapping == "AMBIGUOUS" for e in ctx.entities)


async def test_rename_only_does_not_claim_symbol_behavior_changed(setup):
    _, _, _, builder, _ = setup
    ctx = await builder().build(
        changes(
            changed("new.py", OLD, OLD, status="renamed", previous_path="service.py")
        )
    )
    assert ctx.entities[0].mapping == "FILE_CANDIDATE"
    assert ctx.deltas[0].analysis == "METADATA_ONLY"


async def test_route_body_delta_retains_endpoint_identity(setup):
    graph, source, _, builder, _ = setup
    old = '@router.get("/requests")\n' + OLD
    new = '@router.get("/requests")\n' + NEW
    source.values[BASE, "service.py"] = old
    graph.chunks = [
        {
            "path": "service.py",
            "chunk": chunk(text=old),
            "entities": [entity("endpoint", "GET /requests")],
        }
    ]
    ctx = await builder().build(changes(changed(before=old, after=new)))
    assert ctx.deltas[0].kind == "API"
    assert "GET /requests" in ctx.deltas[0].symbols
    assert ctx.entities[0].mapping == "CHANGED_SYMBOL"


async def test_merge_base_is_separate_from_target_base(setup):
    _, source, _, builder, _ = setup
    source.values[MERGE, "service.py"] = OLD
    source.values[BASE, "service.py"] = "# target-only change\n" + OLD
    ctx = await builder().build(changes(merge_base_sha=MERGE))
    assert ctx.deltas[0].target_comparison == "DIVERGED"
    assert "TARGET_DIVERGED" in {l.code for l in ctx.coverage.limitations}
    target = next(e for e in ctx.evidence if e.origin == "TARGET_BASE")
    old = next(e for e in ctx.evidence if e.origin == "MERGE_BASE")
    assert target.commit_sha == BASE and old.commit_sha == MERGE
    assert "target-only" in target.excerpt and "target-only" not in old.excerpt


async def test_added_deleted_renamed_binary_and_missing_patch(setup):
    graph, source, _, builder, _ = setup
    graph.chunks = [
        {"path": "old.py", "chunk": chunk(path="old.py"), "entities": [entity()]}
    ]
    source.values[BASE, "old.py"] = OLD
    source.values[BASE, "deleted.py"] = OLD
    fixtures = changes(
        changed("new.py", None, "class NewService:\n    pass\n", status="added"),
        changed("deleted.py", OLD, None, status="removed"),
        changed("renamed.py", OLD, OLD, status="renamed", previous_path="old.py"),
        changed(
            "data.bin",
            None,
            None,
            base_content_status="binary",
            head_content_status="binary",
            patch_status="binary",
        ),
    )
    ctx = await builder().build(fixtures)
    assert {d.change_status for d in ctx.deltas} == {
        "added",
        "removed",
        "renamed",
        "modified",
    }
    assert any(
        e.origin == "NEW_CANDIDATE" and e.name == "NewService" for e in ctx.entities
    )
    assert next(f for f in ctx.coverage.files if f.path == "renamed.py").mapped
    assert (
        next(f for f in ctx.coverage.files if f.path == "data.bin").status
        == "METADATA_ONLY"
    )
    deleted = next(d for d in ctx.deltas if d.path == "deleted.py")
    assert deleted.head_location is None
    assert "CONTENT_UNAVAILABLE" in {l.code for l in ctx.coverage.limitations}
    assert any("old.py" in e.paths for e in ctx.entities if e.origin == "CODE")


def edge(src, dst, cid, support, source_type=None):
    return {
        "source": src,
        "target": dst,
        "assertion": {
            "id": cid,
            "repository": REPO,
            "relationshipType": "CALLS",
            "confidence": 0.9,
            "rationale": "Source demonstrates directed call",
            "source_type": source_type,
            "source_id": "adr-1" if source_type == "ADR" else None,
        },
        "support": support,
    }


async def test_relationship_direction_and_provenance_do_not_merge(setup):
    graph, _, _, builder, _ = setup
    code = {"chunk": chunk(), "adr": None, "is_adr": False}
    adr = {**adr_support(), "is_adr": True}
    graph.edges = [
        edge(entity(), entity("consumer", "Consumer"), "code-edge", [code]),
        edge(entity(), entity("consumer", "Consumer"), "adr-edge", [adr], "ADR"),
    ]
    graph.documents = [{"adr": adr["adr"]}]
    ctx = await builder().build(changes())
    assert {(r.source_id, r.target_id, r.provenance) for r in ctx.relationships} == {
        ("process", "consumer", "OBSERVED_CODE"),
        ("process", "consumer", "ADR_ASSERTION"),
    }
    assert len(ctx.relationships) == 2
    assert all(r.assertion_ids and r.evidence_ids for r in ctx.relationships)


async def test_two_hops_are_bounded_and_adr_does_not_propagate(setup):
    graph, _, _, builder, _ = setup
    code = {"chunk": chunk(), "adr": None, "is_adr": False}
    graph.edges = [
        edge(entity(), entity("next", "Next"), "first", [code]),
        edge(entity("next", "Next"), entity("second", "Second"), "second", [code]),
        edge(entity("second", "Second"), entity("third", "Third"), "third", [code]),
    ]
    ctx = await builder(ContextSettings(dependency_hops=2)).build(changes())
    assert {r.assertion_ids[0] for r in ctx.relationships} == {"first", "second"}
    assert "third" not in {e.entity_id for e in ctx.entities}


async def test_stale_chunk_does_not_establish_current_code_relationship(setup):
    graph, source, _, builder, _ = setup
    historical = "d" * 40
    graph.edges = [
        edge(
            entity(),
            entity("consumer", "Consumer"),
            "edge",
            [{"chunk": chunk(commit=historical), "is_adr": False}],
        )
    ]
    source.values[historical, "service.py"] = OLD
    source.values[BASE, "service.py"] = NEW
    ctx = await builder().build(changes())
    assert ctx.relationships[0].provenance == "UNKNOWN"
    assert "BASELINE_CHUNK_DIVERGED" in {l.code for l in ctx.coverage.limitations}


async def test_adr_only_fallback_and_rkg_only(setup):
    graph, _, _, builder, _ = setup
    pure = await builder().build(changes())
    assert pure.coverage_mode == "RKG_ONLY" and pure.coverage.adr_retrieval == "ABSENT"
    support = adr_support()
    graph.documents = [{"adr": support["adr"]}]
    graph.knowledge = [
        requirement(
            targets=[entity("adr-component", "Request Pipeline", source="ADR")]
        ),
        requirement(
            "unrelated", statement="Shipping uses external carrier queues.", targets=[]
        ),
    ]
    graph.knowledge[1]["knowledge"]["title"] = "Shipping"
    ctx = await builder().build(changes())
    candidate = next(r for r in ctx.requirements if r.requirement_id == "decision-1")
    assert candidate.discovery_reasons == ("LEXICAL_FALLBACK",)
    assert candidate.target_entity_ids == ("adr-component",)
    assert candidate.applicability == "UNASSESSED"
    # Missing scope is not evidence of irrelevance: unlinked legacy records
    # remain lower-ranked fallback candidates, rather than being excluded.
    assert next(
        r for r in ctx.requirements if r.requirement_id == "unrelated"
    ).discovery_reasons == ("LEGACY_SCOPE_FALLBACK",)


@pytest.mark.parametrize("failure", ["fail_adr", "fail_requirements"])
async def test_adr_failure_is_degraded_not_absent(setup, failure):
    graph, _, _, builder, _ = setup
    graph.documents = [{"adr": adr_support()["adr"]}]
    setattr(graph, failure, True)
    ctx = await builder().build(changes())
    assert ctx.coverage_mode == "RKG_ADR_DEGRADED"
    assert ctx.coverage.adr_retrieval == "DEGRADED"
    assert not ctx.coverage.adr_catalog_complete


@pytest.mark.parametrize(
    "channel", ["chunk", "entity", "relationship", "adr", "requirement", "source"]
)
async def test_repository_isolation_rejects_leaked_data(setup, channel):
    graph, source, _, builder, _ = setup
    if channel == "chunk":
        graph.chunks[0]["chunk"]["repository"] = "repo_other"
    elif channel == "entity":
        graph.chunks[0]["entities"][0]["repository"] = "repo_other"
    elif channel == "relationship":
        graph.edges = [edge(entity(), entity(repo="repo_other"), "bad", [])]
    elif channel == "adr":
        support = adr_support()
        support["adr"]["repository"] = "repo_other"
        graph.documents = [{"adr": support["adr"]}]
    elif channel == "requirement":
        graph.documents = [{"adr": adr_support()["adr"]}]
        graph.knowledge = [requirement()]
        graph.knowledge[0]["knowledge"]["repository"] = "repo_other"
    else:
        original = source.read_blob
        source.read_blob = lambda repo, sha, path: original(repo, sha, path).model_copy(
            update={"repository_id": "repo_other"}
        )
    if channel == "source":
        ctx = await builder().build(changes())
        assert "TARGET_SOURCE_UNAVAILABLE" in {l.code for l in ctx.coverage.limitations}
        assert not any(
            e.placement == "EXACT" and e.origin == "INDEXED_CODE" for e in ctx.evidence
        )
    else:
        with pytest.raises(RepositoryScopeError):
            await builder().build(changes())


@pytest.mark.parametrize(
    "updates",
    [
        {"indexed_rkg_sha": HEAD},
        {"readiness": "UNKNOWN"},
        {"readiness": "DIRTY"},
        {"repository_status": "INDEXING"},
        {"unfinished_ingestion": True},
        {"tracked_branch": "develop"},
        {"repository_id": "other"},
    ],
)
async def test_baseline_safety_rejects_before_graph_read(setup, updates):
    graph, _, readiness, builder, _ = setup
    readiness.state = readiness.state.model_copy(update=updates)
    with pytest.raises(ContextConstructionError):
        await builder().build(changes())
    assert graph.calls == []


async def test_generation_change_during_materialization_is_rejected(setup):
    graph, _, readiness, builder, _ = setup
    original = graph.file_chunks

    def changed_generation(paths, limit):
        readiness.state = readiness.state.model_copy(update={"graph_generation": 8})
        return original(paths, limit)

    graph.file_chunks = changed_generation
    with pytest.raises(ContextConstructionError, match="changed"):
        await builder().build(changes())


async def test_context_candidate_limits_record_omissions(setup):
    graph, _, _, builder, _ = setup
    graph.chunks[0]["entities"].append(entity("other", "Other"))
    graph.documents = [{"adr": adr_support()["adr"]}]
    graph.knowledge = [requirement(), requirement("decision-2")]
    ctx = await builder(ContextSettings(max_entities=1, max_decisions=1)).build(
        changes()
    )
    assert len(ctx.entities) == 1 and len(ctx.requirements) == 1
    assert {"ENTITY_LIMIT", "DECISION_LIMIT"} <= {
        l.code for l in ctx.coverage.limitations
    }


async def test_constraint_catalog_and_chunk_limits_are_explicit(setup):
    graph, _, _, builder, _ = setup
    graph.chunks.append(
        {"path": "service.py", "chunk": chunk("another"), "entities": [entity()]}
    )
    graph.documents = [{"adr": adr_support()["adr"]}]
    graph.knowledge = [
        requirement("con-1", "CONSTRAINT"),
        requirement("con-2", "CONSTRAINT"),
    ]
    ctx = await builder(
        ContextSettings(max_chunks=1, max_constraints=1, max_catalog_items=1)
    ).build(changes())
    assert len(ctx.requirements) == 1
    assert {
        "CHUNK_LIMIT",
        "DIRECT_REQUIREMENT_LIMIT",
        "FALLBACK_REQUIREMENT_LIMIT",
    } <= {l.code for l in ctx.coverage.limitations}


async def test_target_source_failure_is_not_merge_base_substitution(setup):
    _, source, _, builder, _ = setup
    original = source.read_blob

    def missing(repo, sha, path):
        if sha == BASE:
            raise RuntimeError("target unavailable")
        return original(repo, sha, path)

    source.read_blob = missing
    ctx = await builder().build(changes(merge_base_sha=MERGE))
    assert ctx.deltas[0].target_comparison == "UNAVAILABLE"
    assert (
        next(e for e in ctx.evidence if e.origin == "TARGET_BASE").completeness
        == "UNAVAILABLE"
    )


async def test_corrupt_adr_support_hash_cannot_ground_requirement(setup):
    graph, _, _, builder, _ = setup
    graph.documents = [{"adr": adr_support()["adr"]}]
    graph.knowledge = [requirement()]
    graph.knowledge[0]["support"][0]["chunk"]["contentHash"] = "wrong"
    ctx = await builder().build(changes())
    assert not ctx.requirements[0].evidence_ids
    assert "ADR_HASH_MISMATCH" in {l.code for l in ctx.coverage.limitations}


def test_context_settings_validate_environment(monkeypatch):
    monkeypatch.setenv("DG_CONTEXT_MAX_ENTITIES", "5")
    assert ContextSettings.from_env().max_entities == 5
    monkeypatch.setenv("DG_CONTEXT_DEPENDENCY_HOPS", "3")
    with pytest.raises(ValueError, match="two hops"):
        ContextSettings.from_env()


async def test_total_serialized_budget_and_excerpt_limits(setup):
    _, _, _, builder, _ = setup
    full = await builder().build(changes())
    limited = await builder(
        ContextSettings(
            max_context_tokens=full.budget.estimated_tokens - 1200,
            max_evidence_chars=30,
        )
    ).build(changes())
    assert limited.budget.estimated_tokens <= limited.budget.token_limit
    assert any(
        l.code.endswith("LIMIT") or l.code == "EVIDENCE_EXCERPT"
        for l in limited.coverage.limitations
    )
    ids = {e.evidence_id for e in limited.evidence}
    assert all(
        set(i.evidence_ids) <= ids
        for i in (*limited.entities, *limited.deltas, *limited.requirements)
    )
    with pytest.raises(ContextConstructionError, match="budget"):
        await builder(ContextSettings(max_context_tokens=50)).build(changes())


async def test_lock_is_held_until_cancelled_worker_stops(setup):
    graph, _, _, builder, locks = setup
    entered, release = threading.Event(), threading.Event()
    original = graph.file_chunks

    def blocking(paths, limit):
        entered.set()
        assert release.wait(5)
        return original(paths, limit)

    graph.file_chunks = blocking
    task = asyncio.create_task(builder().build(changes()))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert locks["owner/repo"].locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not locks["owner/repo"].locked()


def test_hunk_parser_validates_counts_and_locations():
    hunks = parse_hunks("diff --git a/a b/a\n@@ -0,0 +1,2 @@\n+first\n+second\n")
    assert hunks[0].old_count == 0 and hunks[0].new_start == 1
    with pytest.raises(ValueError, match="counts"):
        parse_hunks("@@ -1,2 +1,1 @@\n-old\n+new\n")


async def test_invalid_patch_falls_back_to_complete_source(setup):
    _, _, _, builder, _ = setup
    ctx = await builder().build(
        changes(changed(patch="@@ -1,5 +1,5 @@\n-bad\n+patch\n"))
    )
    assert ctx.deltas[0].after == '    return "new"'
    assert "INVALID_PATCH" in {l.code for l in ctx.coverage.limitations}


@pytest.mark.parametrize(
    "path,before,after,kind",
    [
        ("api.py", '@router.get("/x")', '@router.post("/x")', "API"),
        ("models.py", "id = Column(Integer)", "id = Column(String)", "SCHEMA"),
        ("service.py", "db.query(User)", "db.execute(query)", "DATA_ACCESS"),
        (
            "graph.py",
            'graph.add_edge("a", "b")',
            'graph.add_edge("a", "c")',
            "WORKFLOW",
        ),
        ("requirements.txt", "lib==1", "lib==2", "DEPENDENCY"),
        ("config.yaml", "timeout: 5", "timeout: 10", "CONFIGURATION"),
    ],
)
async def test_architectural_delta_classification(setup, path, before, after, kind):
    graph, source, _, builder, _ = setup
    graph.chunks = []
    source.values[BASE, path] = before
    ctx = await builder().build(changes(changed(path, before, after)))
    assert ctx.deltas[0].kind == kind


def test_git_source_adapter_reads_exact_objects_without_mutation(tmp_path):
    source = GitHubRepositorySource(tmp_path / "repos")
    root = source.storage_dir / "owner" / "repo"
    root.mkdir(parents=True)

    def git(*args):
        return (
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.com")
    (root / "-literal.py").write_text(OLD, encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00binary")
    git("add", "-A")
    git("commit", "-m", "base")
    sha = git("rev-parse", "HEAD")
    reader = GitRevisionSourceReader(
        REPO, root, PullRequestGitReader(source, PullRequestSettings())
    )
    before = git("status", "--porcelain")
    assert reader.read_blob(REPO, sha, "-literal.py").text == OLD
    assert reader.read_blob(REPO, sha, "missing.py").status == "absent"
    assert reader.read_blob(REPO, sha, "image.bin").status == "binary"
    assert git("status", "--porcelain") == before and git("rev-parse", "HEAD") == sha
    with pytest.raises(ValueError):
        reader.read_blob("other", sha, "-literal.py")
    with pytest.raises(ValueError):
        reader.read_blob(REPO, sha, "../outside")


def test_neo4j_adapter_uses_read_transaction_and_bound_scope():
    calls = []

    class Tx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class Session(Tx):
        def begin_transaction(self):
            return Tx()

    class Driver:
        def session(self, **kwargs):
            calls.append(kwargs)
            return Session()

    adapter = Neo4jArchitectureGraphReader(Driver(), "test-db")
    with adapter.read_snapshot(REPO) as graph:
        graph.file_chunks(("service.py",), 5)
        graph.relationships(("process",), 5)
        graph.adr_catalog(5)
        graph.communities(("process",), 5)
        graph.applicability_evidence(("adr-1",), ("chunk-1",), 5)
        graph.requirements(("process",), 5, fallback=True)
    assert calls[0] == {"database": "test-db", "default_access_mode": "READ"}
    for query, params in calls[1:]:
        assert params["repo"] == REPO and params["limit"] == 6
        assert "$repo" in query
        assert not any(
            word in query.upper() for word in ("MERGE ", "DELETE ", "CREATE ", "SET ")
        )
