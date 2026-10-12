from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.ingestion.rkg.token_estimation import rough_token_count

from .delta import CachedSource, analyze_delta, clipped, code_evidence
from .models import (
    ADRIdentity,
    AffectedEntityCandidate,
    ArchitecturalRelationship,
    ArchitectureContext,
    BaselineSnapshot,
    ContextBudget,
    ContextCoverage,
    EvidenceSnapshot,
    Limitation,
    RequirementCandidate,
    RequirementEntityLink,
    RevisionIdentity,
    digest,
    stable_id,
)
from .ports import ArchitectureGraphReader, BaselineReadiness, RevisionSourceReader
from .scope import discovery, read_scope, scope_chunk_ids, supported_communities, words
from .settings import ContextSettings

logger = logging.getLogger(__name__)


class ContextConstructionError(RuntimeError):
    pass


class RepositoryScopeError(ContextConstructionError):
    pass


def conservative_tokens(text: str) -> int:
    # The existing chars/4 estimator undercounts unicode/code. Byte count is a
    # conservative envelope; callers can inject their model tokenizer instead.
    return max(rough_token_count(text), len(text.encode("utf-8")))


def _scope(node, repo):
    if node and node.get("repository") != repo:
        raise RepositoryScopeError("Graph data escaped the requested repository")


class ArchitectureContextBuilder:
    def __init__(
        self,
        graph: ArchitectureGraphReader,
        source: RevisionSourceReader,
        readiness: BaselineReadiness,
        repository_lock: Callable[[str], asyncio.Lock],
        settings: ContextSettings | None = None,
        token_counter: Callable[[str], int] = conservative_tokens,
    ):
        self.graph, self.source, self.readiness = graph, source, readiness
        self.repository_lock = repository_lock
        self.settings = settings or ContextSettings.from_env()
        self.token_counter = token_counter

    @staticmethod
    def _require_ready(state: BaselineSnapshot, changes: PullRequestChangeSet):
        if (
            changes.repository_private
            or changes.base_repository_id != changes.github_repository_id
        ):
            raise ContextConstructionError(
                "PR repository identity/public eligibility mismatch"
            )
        if (
            state.repository_id != changes.repository_id
            or state.github_repository_id != str(changes.github_repository_id)
            or state.tracked_branch != changes.base_branch
        ):
            raise ContextConstructionError(
                "PR is outside the tracked repository/branch"
            )
        if (
            state.repository_status != "COMPLETED"
            or state.readiness != "READY"
            or state.unfinished_ingestion
        ):
            raise ContextConstructionError("Graph readiness is not safely established")
        if state.indexed_rkg_sha != changes.base_sha:
            raise ContextConstructionError(
                "Indexed baseline does not match PR target base"
            )

    async def build(self, changes: PullRequestChangeSet) -> ArchitectureContext:
        # Existing ingestion registry keys by full_name. Pass its _get_lock here;
        # a Phase 4.3 coordinator may normalize all users to repository-ID keys.
        async with self.repository_lock(changes.repository_full_name):
            baseline = await self.readiness.snapshot(changes.repository_id)
            self._require_ready(baseline, changes)
            worker = asyncio.create_task(
                asyncio.to_thread(self._materialize, changes, baseline)
            )
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Object reads continue in a thread; do not release mutation lock
                # while that thread is still reading graph and clone state.
                try:
                    await worker
                except Exception as exc:  # noqa: BLE001 - preserve cancellation after draining the read worker
                    logger.debug(
                        "Cancelled context worker ended with error_type=%s",
                        type(exc).__name__,
                    )
                raise
            after = await self.readiness.snapshot(changes.repository_id)
            self._require_ready(after, changes)
            if after != baseline:
                raise ContextConstructionError(
                    "Baseline changed during materialization"
                )
            return result

    def _materialize(self, changes, baseline):
        repo, settings = changes.repository_id, self.settings
        source = CachedSource(self.source, repo)
        analysis = analyze_delta(changes, source, settings)
        evidence, limitations = analysis.evidence, analysis.limitations
        entities: dict[str, AffectedEntityCandidate] = {}
        relationships = {}
        requirements = []
        catalog = []
        scope_fingerprints = {}
        baseline_verified_evidence = set()
        adr_status, catalog_complete = "ABSENT", True
        mapping_strength = {
            "CHANGED_SYMBOL": 0,
            "NEW_CANDIDATE": 1,
            "CHANGED_CHUNK": 2,
            "AMBIGUOUS": 3,
            "FILE_CANDIDATE": 4,
            "NEIGHBOR": 5,
        }

        def limitation(code, message, paths=(), ids=()):
            limitations.append(
                Limitation(
                    code=code, message=message, paths=tuple(paths), item_ids=tuple(ids)
                )
            )

        def page_limit(page, channel):
            if page.truncated:
                limitation(
                    channel + "_LIMIT",
                    "Graph retrieval truncated; additional relevant items may exist",
                )

        def keep_entity(
            node,
            mapping="NEIGHBOR",
            *,
            paths=(),
            ev=(),
            ds=(),
            distance=1,
            reason="Directed architectural neighbor",
            code=False,
        ):
            _scope(node, repo)
            eid = node.get("id")
            if not eid or not node.get("name") or not node.get("label"):
                limitation("ENTITY_INCOMPLETE", "Entity identity/type missing")
                return False
            if eid not in entities and len(entities) >= settings.max_entities:
                weakest = max(
                    entities.values(),
                    key=lambda e: (
                        e.distance,
                        mapping_strength[e.mapping],
                        e.entity_id,
                    ),
                )
                if (distance, mapping_strength[mapping]) < (
                    weakest.distance,
                    mapping_strength[weakest.mapping],
                ):
                    del entities[weakest.entity_id]
                    limitation(
                        "ENTITY_LIMIT",
                        "Weaker entity candidate omitted",
                        weakest.paths,
                        (weakest.entity_id,),
                    )
                else:
                    limitation(
                        "ENTITY_LIMIT", "Entity candidate omitted", paths, (eid,)
                    )
                    return False
            item = AffectedEntityCandidate(
                entity_id=eid,
                name=node["name"],
                entity_type=node["label"],
                aliases=tuple(node.get("aliases") or ()),
                origin="CODE"
                if code
                else "ADR_ONLY"
                if node.get("source") == "ADR"
                else "UNKNOWN",
                mapping=mapping,
                paths=tuple(sorted(set(paths))),
                evidence_ids=tuple(sorted(set(ev))),
                delta_ids=tuple(sorted(set(ds))),
                distance=distance,
                reason=reason,
            )
            existing = entities.get(eid)
            if existing:
                winner = (
                    item
                    if mapping_strength[item.mapping]
                    < mapping_strength[existing.mapping]
                    else existing
                )
                item = winner.model_copy(
                    update={
                        "paths": tuple(sorted(set(item.paths + existing.paths))),
                        "evidence_ids": tuple(
                            sorted(set(item.evidence_ids + existing.evidence_ids))
                        ),
                        "delta_ids": tuple(
                            sorted(set(item.delta_ids + existing.delta_ids))
                        ),
                        "distance": min(item.distance, existing.distance),
                        "origin": "CODE"
                        if code or existing.origin == "CODE"
                        else winner.origin,
                    }
                )
            entities[eid] = item
            return True

        def chunk_evidence(chunk):
            _scope(chunk, repo)
            text, path, sha = (
                chunk.get("text"),
                chunk.get("filePath"),
                chunk.get("commit"),
            )
            if text is None or not path or not sha:
                limitation(
                    "CODE_EVIDENCE_INCOMPLETE",
                    "Chunk lacks source identity or text",
                    ids=(chunk.get("id", "unknown"),),
                )
                return None
            placement, start = "UNLOCATED", None
            if chunk.get("contentHash") != digest(text):
                limitation(
                    "CHUNK_HASH_MISMATCH",
                    "Stored chunk hash disagrees with original text",
                    (path,),
                    (chunk["id"],),
                )
            else:
                try:
                    blob = source.read(sha, path)
                    if blob.status == "complete":
                        occurrences = blob.text.count(text) if text else 0
                        if occurrences == 1:
                            placement = "EXACT"
                            start = blob.text[: blob.text.index(text)].count("\n") + 1
                        elif occurrences > 1:
                            placement = "AMBIGUOUS"
                except Exception as exc:  # noqa: BLE001 - source port failures become sanitized coverage diagnostics
                    limitation(
                        "INDEXED_SOURCE_UNAVAILABLE",
                        type(exc).__name__,
                        (path,),
                        (chunk["id"],),
                    )
            if placement != "EXACT":
                limitation(
                    "CHUNK_" + placement,
                    "Chunk cannot be uniquely placed in exact source",
                    (path,),
                    (chunk["id"],),
                )
            ev = code_evidence(
                repo,
                "INDEXED_CODE",
                path,
                sha,
                text,
                settings,
                start=start,
                chunk_id=chunk["id"],
                placement=placement,
                content_hash=chunk.get("contentHash"),
            )
            evidence[ev.evidence_id] = ev
            try:
                target = source.read(changes.base_sha, path)
                if (
                    placement == "EXACT"
                    and target.text is not None
                    and text
                    and target.text.count(text) == 1
                ):
                    baseline_verified_evidence.add(ev.evidence_id)
                else:
                    limitation(
                        "BASELINE_CHUNK_DIVERGED",
                        "Stored chunk is not uniquely corroborated in target-base source",
                        (path,),
                        (chunk["id"],),
                    )
            except Exception as exc:  # noqa: BLE001 - source port failures become sanitized coverage diagnostics
                limitation(
                    "BASELINE_SOURCE_UNAVAILABLE",
                    type(exc).__name__,
                    (path,),
                    (chunk["id"],),
                )
            if ev.completeness == "EXCERPT":
                limitation(
                    "EVIDENCE_LIMIT",
                    "Code evidence was excerpted",
                    (path,),
                    (ev.evidence_id,),
                )
            return ev

        def adr_evidence(support):
            chunk, adr = support.get("chunk"), support.get("adr")
            if not chunk or not adr:
                limitation(
                    "ADR_EVIDENCE_MISSING",
                    "Requirement/assertion has no original supporting ADR chunk",
                )
                return None
            _scope(chunk, repo)
            _scope(adr, repo)
            if (
                chunk.get("adr_id") != adr.get("id")
                or not adr.get("content_hash")
                or chunk.get("text") is None
            ):
                limitation(
                    "ADR_PROVENANCE_INCOMPLETE",
                    "ADR chunk/document identity is incomplete",
                )
                return None
            if chunk.get("contentHash") != digest(chunk["text"]):
                limitation(
                    "ADR_HASH_MISMATCH",
                    "ADR supporting text hash mismatch",
                    ids=(chunk.get("id", "unknown"),),
                )
                return None
            text = clipped(chunk["text"], settings.max_evidence_chars)
            ev = EvidenceSnapshot(
                evidence_id=stable_id(
                    repo,
                    "ADR",
                    adr["id"],
                    adr["content_hash"],
                    chunk["id"],
                    digest(text),
                ),
                repository_id=repo,
                kind="ADR",
                origin="ADR_DOCUMENT",
                adr_id=adr["id"],
                chunk_id=chunk["id"],
                document_hash=adr["content_hash"],
                content_hash=chunk["contentHash"],
                section=chunk.get("section"),
                source_name=adr.get("source_name"),
                source_type=adr.get("source_type"),
                source_url=adr.get("source_url"),
                source_version=adr.get("source_version"),
                excerpt=text,
                excerpt_hash=digest(text),
                completeness="EXCERPT"
                if len(text) < len(chunk["text"])
                else "COMPLETE",
                placement="NOT_APPLICABLE",
            )
            evidence[ev.evidence_id] = ev
            if ev.completeness == "EXCERPT":
                limitation(
                    "ADR_EVIDENCE_LIMIT",
                    "Original ADR text was excerpted",
                    ids=(ev.evidence_id,),
                )
            return ev

        with self.graph.read_snapshot(repo) as graph:
            paths = tuple(
                sorted(
                    {p for f in changes.files for p in (f.path, f.previous_path) if p}
                )
            )
            chunks = graph.file_chunks(paths, settings.max_chunks)
            page_limit(chunks, "CHUNK")
            for row in chunks.rows:
                if row["path"] not in paths:
                    raise RepositoryScopeError(
                        "Graph returned an unrequested changed path"
                    )
                chunk = row["chunk"]
                if chunk.get("filePath") != row["path"] or chunk.get("adr_id"):
                    raise RepositoryScopeError(
                        "Changed-file channel contains mismatched code provenance"
                    )
                ev = chunk_evidence(chunk)
                relevant = [
                    d
                    for d in analysis.deltas
                    if row["path"] in {d.path, d.previous_path}
                ]
                # Recover placement on TARGET_BASE as well; creation-commit line
                # offsets must not be compared directly to merge-base patch lines.
                overlapping = []
                for delta in relevant:
                    try:
                        blob = source.read(changes.merge_base_sha, row["path"])
                        text = chunk.get("text") or ""
                        if (
                            ev
                            and ev.placement == "EXACT"
                            and blob.text
                            and text
                            and blob.text.count(text) == 1
                        ):
                            first = blob.text[: blob.text.index(text)].count("\n") + 1
                            last = first + len(text.splitlines()) - 1
                            if (
                                delta.analysis == "TEXT"
                                and delta.old_location
                                and first <= delta.old_location.end_line
                                and last >= delta.old_location.start_line
                            ):
                                overlapping.append(delta)
                    except Exception as exc:  # noqa: BLE001 - this read port may use different source backends
                        limitation(
                            "MERGE_BASE_MAPPING_UNAVAILABLE",
                            type(exc).__name__,
                            (row["path"],),
                            (chunk.get("id", "unknown"),),
                        )
                nodes = tuple(
                    sorted(
                        (n for n in row.get("entities", ()) if n),
                        key=lambda n: n.get("id", ""),
                    )
                )
                for node in nodes:
                    names = {
                        node.get("name", "").casefold(),
                        *(a.casefold() for a in node.get("aliases") or ()),
                    }
                    matched = []
                    for delta in overlapping:
                        unique_symbols = {
                            s.casefold()
                            for s in delta.symbols
                            if sum(
                                s.casefold()
                                in {
                                    n.get("name", "").casefold(),
                                    *(a.casefold() for a in n.get("aliases") or ()),
                                }
                                for n in nodes
                            )
                            == 1
                        }
                        if names.intersection(unique_symbols):
                            matched.append(delta)
                    mapping = (
                        "CHANGED_SYMBOL"
                        if matched
                        else "AMBIGUOUS"
                        if not ev or ev.placement != "EXACT"
                        else "CHANGED_CHUNK"
                        if overlapping and len(nodes) == 1
                        else "AMBIGUOUS"
                        if overlapping
                        else "FILE_CANDIDATE"
                    )
                    keep_entity(
                        node,
                        mapping,
                        paths=(row["path"],),
                        ev=(ev.evidence_id,) if ev else (),
                        ds=tuple(d.delta_id for d in matched or overlapping),
                        distance=0,
                        reason="Source-symbol match"
                        if matched
                        else "Chunk mentions are candidates, not proof of changed behavior",
                        code=True,
                    )
            # Added or newly introduced declarations stay local to this context.
            for delta in analysis.deltas:
                old_symbols = {e.name.casefold() for e in entities.values()}
                for name in delta.symbols:
                    if name.casefold() in old_symbols:
                        continue
                    file = next(f for f in changes.files if f.path == delta.path)
                    if file.head_content_status != "complete" or not re.search(
                        r"\b(?:class|def)\s+" + re.escape(name) + r"\b",
                        file.head_content or "",
                    ):
                        continue
                    if re.search(
                        r"\b(?:class|def)\s+" + re.escape(name) + r"\b",
                        file.base_content or "",
                    ):
                        continue
                    eid = "new:" + stable_id(repo, changes.head_sha, delta.path, name)
                    if len(entities) >= settings.max_entities:
                        limitation(
                            "ENTITY_LIMIT",
                            "New component candidate omitted",
                            (delta.path,),
                            (eid,),
                        )
                        continue
                    existing = entities.get(eid)
                    ds = tuple(
                        sorted(
                            {delta.delta_id, *(existing.delta_ids if existing else ())}
                        )
                    )
                    ev_ids = tuple(
                        sorted(
                            {
                                *delta.evidence_ids,
                                *(existing.evidence_ids if existing else ()),
                            }
                        )
                    )
                    entities[eid] = AffectedEntityCandidate(
                        entity_id=eid,
                        name=name,
                        entity_type="Component",
                        origin="NEW_CANDIDATE",
                        mapping="NEW_CANDIDATE",
                        paths=(delta.path,),
                        evidence_ids=ev_ids,
                        delta_ids=ds,
                        distance=0,
                        reason="New declaration has no mapped baseline identity; repository-wide equivalence is unverified",
                    )
            frontier = tuple(
                sorted(
                    eid for eid, e in entities.items() if e.origin != "NEW_CANDIDATE"
                )
            )
            for hop in range(1, settings.dependency_hops + 1):
                if not frontier:
                    break
                page = graph.relationships(frontier, settings.max_relationships)
                page_limit(page, "RELATIONSHIP")
                next_ids = set()
                for row in page.rows:
                    assertion, src, dst = row["assertion"], row["source"], row["target"]
                    for node in (assertion, src, dst):
                        _scope(node, repo)
                    if not ({src.get("id"), dst.get("id")} & set(frontier)):
                        raise RepositoryScopeError(
                            "Graph returned an unseeded relationship"
                        )
                    if not assertion.get("relationshipType"):
                        limitation(
                            "RELATIONSHIP_INCOMPLETE",
                            "Assertion has no relationship type",
                        )
                        continue
                    rid = stable_id(repo, assertion["id"])
                    if rid in relationships:
                        continue
                    if len(relationships) >= settings.max_relationships:
                        limitation(
                            "RELATIONSHIP_LIMIT", "Relationship omitted", ids=(rid,)
                        )
                        continue
                    code, adr = [], []
                    for support in row.get("support", ()):
                        c = support.get("chunk")
                        if not c:
                            continue
                        _scope(c, repo)
                        if support.get("is_adr") or c.get("adr_id"):
                            if assertion.get("source_type") != "ADR" or assertion.get(
                                "source_id"
                            ) != c.get("adr_id"):
                                limitation(
                                    "MIXED_ASSERTION_PROVENANCE",
                                    "ADR support does not establish observed code",
                                )
                                continue
                            value = adr_evidence(support)
                            if value:
                                adr.append(value.evidence_id)
                        else:
                            value = chunk_evidence(c)
                            if (
                                value
                                and value.evidence_id in baseline_verified_evidence
                            ):
                                code.append(value.evidence_id)
                    provenance = (
                        "ADR_ASSERTION"
                        if assertion.get("source_type") == "ADR"
                        else "OBSERVED_CODE"
                        if code
                        else "UNKNOWN"
                    )
                    if provenance == "UNKNOWN":
                        limitation(
                            "UNSUPPORTED_RELATIONSHIP",
                            "No verified code support for observed relationship",
                            ids=(assertion["id"],),
                        )
                    atext = clipped(
                        assertion.get("rationale") or "", settings.max_evidence_chars
                    )
                    aev = EvidenceSnapshot(
                        evidence_id=stable_id(
                            repo, "ASSERTION", assertion["id"], digest(atext)
                        ),
                        repository_id=repo,
                        kind="GRAPH_ASSERTION",
                        origin="ADR_DOCUMENT"
                        if provenance == "ADR_ASSERTION"
                        else "INDEXED_CODE",
                        assertion_id=assertion["id"],
                        adr_id=assertion.get("source_id"),
                        excerpt=atext,
                        excerpt_hash=digest(atext),
                        completeness="EXCERPT"
                        if len(atext) < len(assertion.get("rationale") or "")
                        else "COMPLETE",
                        placement="NOT_APPLICABLE",
                    )
                    evidence[aev.evidence_id] = aev
                    ev_ids = tuple(sorted(set(code + adr + [aev.evidence_id])))
                    for node in (src, dst):
                        if node["id"] not in entities:
                            next_ids.add(node["id"])
                        keep_entity(
                            node,
                            ev=ev_ids,
                            distance=hop,
                            code=provenance == "OBSERVED_CODE",
                        )
                    if src["id"] in entities and dst["id"] in entities:
                        relationships[rid] = ArchitecturalRelationship(
                            relationship_id=rid,
                            source_id=src["id"],
                            target_id=dst["id"],
                            relationship_type=assertion["relationshipType"],
                            provenance=provenance,
                            assertion_ids=(assertion["id"],),
                            evidence_ids=ev_ids,
                            confidence=float(assertion["confidence"])
                            if assertion.get("confidence") is not None
                            else None,
                            rationale=atext,
                        )
                # ADR assertions inform intent, not dependency propagation.
                frontier = tuple(
                    sorted(
                        eid
                        for eid in next_ids
                        if eid in entities
                        and any(
                            r.provenance == "OBSERVED_CODE"
                            and eid in {r.source_id, r.target_id}
                            for r in relationships.values()
                        )
                    )
                )
            try:
                documents = graph.adr_catalog(settings.max_adr_documents)
                catalog_complete = not documents.truncated
                page_limit(documents, "ADR_CATALOG")
                for row in documents.rows:
                    doc = row["adr"]
                    _scope(doc, repo)
                    if not doc.get("id") or not doc.get("content_hash"):
                        raise ValueError("ADR catalog lacks source identity")
                    catalog.append(
                        ADRIdentity(adr_id=doc["id"], content_hash=doc["content_hash"])
                    )
                adr_status = "AVAILABLE" if catalog or documents.truncated else "ABSENT"
                if adr_status == "ABSENT" and any(
                    r.provenance == "ADR_ASSERTION" for r in relationships.values()
                ):
                    raise ValueError(
                        "ADR assertions exist without a matching ADR catalog"
                    )
                if adr_status != "ABSENT":
                    seeds = tuple(
                        sorted(
                            eid
                            for eid, e in entities.items()
                            if e.origin != "NEW_CANDIDATE"
                        )
                    )
                    community_entities, communities = {}, {}
                    associations = graph.communities(
                        seeds, settings.max_community_associations
                    )
                    page_limit(associations, "COMMUNITY_ASSOCIATION")
                    for association in associations.rows:
                        member, community = (
                            association["entity"],
                            association["community"],
                        )
                        _scope(member, repo)
                        _scope(community, repo)
                        if member.get("id") not in seeds or not community.get(
                            "communityId"
                        ):
                            raise RepositoryScopeError(
                                "Community association escaped the seeded entities"
                            )
                        cid = str(community["communityId"])
                        community_entities.setdefault(
                            member["id"], {**member, "community_ids": []}
                        )["community_ids"].append(cid)
                        communities[cid] = {
                            "id": cid,
                            "name": community.get("name"),
                            "title": community.get("title"),
                            "architectural_role": community.get("architecturalRole"),
                            "aliases": community.get("aliases") or [],
                        }
                    language_map = {
                        "py": "python",
                        "js": "javascript",
                        "ts": "typescript",
                        "tsx": "typescript",
                        "java": "java",
                        "go": "go",
                        "rs": "rust",
                        "rb": "ruby",
                    }
                    languages = tuple(
                        sorted(
                            {
                                language_map[f.path.rsplit(".", 1)[-1]]
                                for f in changes.files
                                if f.path.rsplit(".", 1)[-1] in language_map
                            }
                        )
                    )
                    community_ids = tuple(sorted(communities))
                    direct = graph.requirements(
                        seeds,
                        settings.max_catalog_items,
                        community_ids=community_ids,
                        languages=languages,
                    )
                    fallback = graph.requirements(
                        seeds,
                        settings.max_catalog_items,
                        fallback=True,
                        community_ids=community_ids,
                        languages=languages,
                    )
                    page_limit(direct, "DIRECT_REQUIREMENT")
                    page_limit(fallback, "FALLBACK_REQUIREMENT")
                    tokens = words(
                        " ".join(
                            [e.name for e in entities.values()]
                            + [
                                d.path
                                + " "
                                + " ".join(d.symbols)
                                + " "
                                + (d.before or "")
                                + " "
                                + (d.after or "")
                                for d in analysis.deltas
                            ]
                        )
                    )
                    tokens -= {
                        "def",
                        "class",
                        "return",
                        "none",
                        "true",
                        "false",
                        "self",
                        "import",
                        "from",
                        "backend",
                        "app",
                    }
                    rows = {
                        r["knowledge"]["id"]: r for r in (*fallback.rows, *direct.rows)
                    }
                    records, original_evidence, original_adrs = {}, {}, {}
                    requested_chunks, requested_adrs = set(), set()
                    for kid, row in sorted(rows.items()):
                        node = row["knowledge"]
                        _scope(node, repo)
                        for target in row.get("targets", ()):
                            _scope(target, repo)
                        record = read_scope(node)
                        records[kid] = record
                        original_evidence[kid], original_adrs[kid] = [], []
                        for support in row.get("support", ()):
                            if support.get("adr"):
                                _scope(support["adr"], repo)
                                aid = support["adr"]["id"]
                                original_adrs[kid].append(aid)
                                scope_fingerprints.setdefault(aid, {})[kid] = (
                                    record.fingerprint
                                )
                            value = adr_evidence(support)
                            if value:
                                original_evidence[kid].append(value)
                        if record.status == "INVALID":
                            limitation(
                                "INVALID_SCOPE_METADATA",
                                "Persisted applicability is invalid or from an unsupported schema",
                                ids=(kid,),
                            )
                        if record.metadata.context_truncated:
                            limitation(
                                "SCOPE_CONTEXT_TRUNCATED",
                                "Enrichment retained truncated source context",
                                ids=(kid,),
                            )
                        requested_chunks.update(scope_chunk_ids(record.metadata))
                        requested_adrs.update(original_adrs[kid])
                    extra_evidence = {}
                    if requested_chunks:
                        extra = graph.applicability_evidence(
                            tuple(sorted(requested_adrs)),
                            tuple(sorted(requested_chunks)),
                            settings.max_scope_evidence_chunks,
                        )
                        page_limit(extra, "SCOPE_EVIDENCE")
                        for support in extra.rows:
                            chunk, doc = support.get("chunk"), support.get("adr")
                            if (
                                not chunk
                                or not doc
                                or chunk.get("id") not in requested_chunks
                                or doc.get("id") not in requested_adrs
                            ):
                                raise RepositoryScopeError(
                                    "Applicability evidence escaped the requested sources"
                                )
                            value = adr_evidence(support)
                            if value:
                                extra_evidence[doc["id"], chunk["id"]] = value
                    selected = {}
                    direct_ids = {r["knowledge"]["id"] for r in direct.rows}
                    for kid, row in sorted(rows.items()):
                        node = row["knowledge"]
                        record = records[kid]
                        scope_values = tuple(
                            {
                                ev.evidence_id: ev
                                for ev in (
                                    *original_evidence[kid],
                                    *(
                                        extra_evidence[aid, cid]
                                        for aid in original_adrs[kid]
                                        for cid in scope_chunk_ids(record.metadata)
                                        if (aid, cid) in extra_evidence
                                    ),
                                )
                            }.values()
                        )
                        owned_chunks = {ev.chunk_id for ev in scope_values}
                        missing = set(scope_chunk_ids(record.metadata)) - owned_chunks
                        if missing:
                            limitation(
                                "SCOPE_EVIDENCE_MISSING",
                                "Scope evidence could not be verified within its defining ADRs",
                                ids=(kid, *sorted(missing)),
                            )
                        supported_ids = supported_communities(
                            record.metadata,
                            community_entities,
                            communities,
                            owned_chunks,
                        )
                        if (
                            set(record.metadata.community_ids) & set(community_ids)
                            and not supported_ids
                        ):
                            limitation(
                                "UNVERIFIED_COMMUNITY_SCOPE",
                                "Stored association is a retrieval hint; semantic scope/evidence is not justified",
                                ids=(kid,),
                            )
                        adr_mention = row.get(
                            "adr_mention",
                            kid in direct_ids
                            and not any(
                                t.get("id") in seeds for t in row.get("targets", ())
                            ),
                        )
                        reasons, score = discovery(
                            record,
                            node,
                            row.get("targets", ()),
                            seeds,
                            supported_ids,
                            adr_mention=adr_mention,
                            tokens=tokens,
                            languages=languages,
                            paths=tuple(f.path for f in changes.files),
                        )
                        selected[kid] = (
                            score,
                            reasons,
                            row,
                            scope_values,
                            supported_ids,
                        )
                    counts = {"DECISION": 0, "CONSTRAINT": 0}
                    for _, reasons, row, scope_values, supported_ids in sorted(
                        selected.values(),
                        key=lambda v: (-v[0], v[2]["knowledge"]["id"]),
                    ):
                        kind = row["kind"]
                        limit = (
                            settings.max_decisions
                            if kind == "DECISION"
                            else settings.max_constraints
                        )
                        node = row["knowledge"]
                        if counts[kind] >= limit:
                            limitation(
                                kind + "_LIMIT",
                                "Relevant requirement candidate omitted",
                                ids=(node["id"],),
                            )
                            continue
                        record = records[node["id"]]
                        scope_evidence_ids = tuple(
                            sorted(
                                ev.evidence_id
                                for ev in scope_values
                                if ev.chunk_id in scope_chunk_ids(record.metadata)
                            )
                        )
                        ev_ids = tuple(sorted({ev.evidence_id for ev in scope_values}))
                        adr_ids = original_adrs[node["id"]]
                        counts[kind] += 1
                        requirements.append(
                            RequirementCandidate(
                                requirement_id=node["id"],
                                kind=kind,
                                title=node.get("title"),
                                statement=node.get("description") or "",
                                requirement_type=node.get("decision_type")
                                or node.get("constraint_type"),
                                adr_ids=tuple(sorted(set(adr_ids))),
                                target_entity_ids=tuple(
                                    sorted(
                                        {
                                            t["id"]
                                            for t in row.get("targets", ())
                                            if t.get("id")
                                        }
                                    )
                                ),
                                target_links=tuple(
                                    RequirementEntityLink(
                                        entity_id=t["id"],
                                        name=t["name"],
                                        entity_type=t["label"],
                                        relationship=t["relationship"],
                                        origin="CODE"
                                        if t["id"] in entities
                                        and entities[t["id"]].origin == "CODE"
                                        else "ADR_ONLY"
                                        if t.get("source") == "ADR"
                                        else "UNKNOWN",
                                    )
                                    for t in sorted(
                                        row.get("targets", ()),
                                        key=lambda t: (
                                            t.get("id", ""),
                                            t.get("relationship", ""),
                                        ),
                                    )
                                    if t.get("id")
                                    and t.get("name")
                                    and t.get("label")
                                    and t.get("relationship")
                                    in {"AFFECTS", "CONSTRAINS"}
                                ),
                                evidence_ids=ev_ids,
                                discovery_reasons=reasons,
                                relevance_score=selected[node["id"]][0],
                                declared_applicability_json=record.metadata_json,
                                scope_metadata_status=record.status,
                                enrichment_fingerprint=record.fingerprint,
                                enrichment_status=record.enrichment_status,
                                enrichment_model=record.model,
                                supported_community_ids=supported_ids,
                                scope_evidence_ids=scope_evidence_ids,
                            )
                        )
            except RepositoryScopeError:
                raise
            except Exception as exc:  # noqa: BLE001 - ADR read/validation failures must degrade rather than imply absence
                adr_status, catalog_complete = "DEGRADED", False
                limitation("ADR_RETRIEVAL_FAILED", type(exc).__name__)
        coverage = [
            f.model_copy(
                update={
                    "mapped": any(
                        f.path in e.paths
                        or any(
                            d.path == f.path and d.previous_path in e.paths
                            for d in analysis.deltas
                        )
                        for e in entities.values()
                        if e.origin == "CODE"
                    )
                }
            )
            for f in analysis.coverage
        ]
        for file in coverage:
            if not file.mapped:
                limitation(
                    "RKG_UNMAPPED_FILE",
                    "No retained code-backed entity candidate for changed path",
                    (file.path,),
                )
        catalog = [
            doc.model_copy(
                update={
                    "applicability_fingerprint": digest(
                        json.dumps(
                            scope_fingerprints.get(doc.adr_id, {}), sort_keys=True
                        )
                    )
                }
            )
            for doc in catalog
        ]
        return self._finish(
            changes,
            baseline,
            catalog,
            catalog_complete,
            adr_status,
            analysis.deltas,
            list(entities.values()),
            list(relationships.values()),
            requirements,
            evidence,
            coverage,
            limitations,
        )

    def _finish(
        self,
        changes,
        baseline,
        catalog,
        catalog_complete,
        adr_status,
        deltas,
        entities,
        relationships,
        requirements,
        evidence,
        files,
        limitations,
    ):
        """Prune whole candidate units, retaining explicit omissions and valid references."""
        revision = RevisionIdentity(
            repository_id=changes.repository_id,
            github_repository_id=str(changes.github_repository_id),
            pull_request_number=changes.pull_request_number,
            base_sha=changes.base_sha,
            head_sha=changes.head_sha,
            merge_base_sha=changes.merge_base_sha,
            revision_key=changes.revision_key,
        )
        mode = (
            "RKG_ONLY"
            if adr_status == "ABSENT"
            else "RKG_ADR"
            if adr_status == "AVAILABLE"
            else "RKG_ADR_DEGRADED"
        )
        catalog = tuple(sorted(set(catalog), key=lambda a: a.adr_id))
        catalog_hash = digest(
            json.dumps([a.model_dump() for a in catalog], sort_keys=True)
        )

        def payload():
            used = {
                eid
                for item in (*deltas, *entities, *relationships, *requirements)
                for eid in item.evidence_ids
            }
            # Context records the immutable selected evidence, not unused retrievals.
            return {
                "revision": revision,
                "baseline": baseline,
                "coverage_mode": mode,
                "adr_snapshot": catalog,
                "adr_catalog_hash": catalog_hash,
                "deltas": tuple(sorted(deltas, key=lambda d: d.delta_id)),
                "entities": tuple(sorted(entities, key=lambda e: e.entity_id)),
                "relationships": tuple(
                    sorted(relationships, key=lambda r: r.relationship_id)
                ),
                "requirements": tuple(
                    sorted(
                        requirements,
                        key=lambda r: (-r.relevance_score, r.requirement_id),
                    )
                ),
                "evidence": tuple(
                    sorted(
                        (e for k, e in evidence.items() if k in used),
                        key=lambda e: e.evidence_id,
                    )
                ),
                "coverage": ContextCoverage(
                    files=tuple(files),
                    adr_retrieval=adr_status,
                    adr_catalog_complete=catalog_complete,
                    limitations=tuple(
                        sorted(
                            set(limitations),
                            key=lambda l: (l.code, l.paths, l.item_ids, l.message),
                        )
                    ),
                ),
                "budget": ContextBudget(
                    token_limit=self.settings.max_context_tokens,
                    estimated_tokens=0,
                    estimator="conservative_utf8_bytes"
                    if self.token_counter is conservative_tokens
                    else getattr(self.token_counter, "name", "injected_token_counter"),
                ),
                "context_hash": "0" * 64,
            }

        def assemble():
            data = payload()
            ctx = ArchitectureContext(**data)
            # Hash excludes only self-referential hash and budget accounting.
            hashed = ctx.model_dump(mode="json", exclude={"context_hash", "budget"})
            ctx = ctx.model_copy(
                update={
                    "context_hash": digest(
                        json.dumps(hashed, sort_keys=True, ensure_ascii=False)
                    )
                }
            )
            estimate = 0
            for _ in range(16):
                ctx = ctx.model_copy(
                    update={
                        "budget": ctx.budget.model_copy(
                            update={"estimated_tokens": estimate}
                        )
                    }
                )
                measured = self.token_counter(ctx.model_dump_json())
                if type(measured) is not int or measured < 0:
                    raise ContextConstructionError(
                        "Token counter must return a nonnegative integer"
                    )
                if measured <= estimate:
                    break
                estimate = max(estimate, measured)
            else:
                raise ContextConstructionError(
                    "Serialized token accounting failed to converge"
                )
            return ctx.model_copy(
                update={
                    "budget": ctx.budget.model_copy(
                        update={"estimated_tokens": estimate}
                    )
                }
            )

        omitted = set()
        while True:
            ctx = assemble()
            if ctx.budget.estimated_tokens <= self.settings.max_context_tokens:
                return ArchitectureContext.model_validate(ctx.model_dump())
            if requirements:
                item = min(
                    requirements, key=lambda r: (r.relevance_score, r.requirement_id)
                )
                requirements.remove(item)
            elif relationships:
                relationships.pop()
            elif entities:
                item = max(
                    entities,
                    key=lambda e: (
                        e.distance,
                        e.mapping != "CHANGED_SYMBOL",
                        e.entity_id,
                    ),
                )
                entities.remove(item)
            elif deltas:
                item = deltas.pop()
                omitted.add(item.path)
                files = [
                    f.model_copy(
                        update={
                            "status": "BUDGET_OMITTED",
                            "reasons": tuple(
                                sorted(set(f.reasons + ("CONTEXT_TOKEN_LIMIT",)))
                            ),
                        }
                    )
                    if f.path in omitted
                    else f
                    for f in files
                ]
            else:
                raise ContextConstructionError(
                    "Token budget cannot hold revision, snapshot and complete file coverage metadata"
                )
            limitations.append(
                Limitation(
                    code="CONTEXT_TOKEN_LIMIT",
                    message="Context candidates/evidence omitted to meet total serialized budget",
                )
            )
