"""Derived repository search snapshots. Original graph/ADR records are read-only."""

import asyncio
import json
import logging
import time
from types import SimpleNamespace

from ..context import ContextConstructionError, _scope
from ..delta import CachedSource
from ..models import (
    ADRIdentity,
    Limitation,
    RequirementCandidate,
    RequirementEntityLink,
    stable_id,
)
from ..original_evidence import original_evidence_resolvers
from ..scope import read_scope, scope_chunk_ids
from ..settings import ContextSettings
from .chunks import UploadedADRChunkVerifier, collect_chunk_vectors
from .lexical import tokenize
from .models import (
    CommunityIdentity,
    IndexSettings,
    SearchIndex,
    SearchUnit,
    confirmed_legacy_manifest,
    fingerprint,
)

logger = logging.getLogger(__name__)


class BoundedSources(CachedSource):
    def __init__(self, source, repository_id, settings, deadline):
        super().__init__(source, repository_id)
        self.settings, self.bytes = settings, 0
        self.deadline = deadline

    def read(self, sha, path):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Index source deadline")
        key = sha, path
        if (
            self.bytes > self.settings.max_source_bytes
            or key not in self.cache
            and len(self.cache) >= self.settings.max_source_reads
        ):
            raise ValueError("Index source read budget")
        new = key not in self.cache
        blob = super().read(sha, path)
        if new:
            self.bytes += len((blob.text or "").encode("utf-8"))
        if self.bytes > self.settings.max_source_bytes:
            raise ValueError("Index source byte budget")
        return blob


class DerivedIndexBuilder:
    def __init__(
        self,
        graph,
        source,
        authority=None,
        *,
        settings=None,
        readiness=None,
        repository_lock=None,
        mode="LIVE",
        compatibility_manifest=None,
        chunk_verifier=None,
        store=None,
    ):
        self.graph, self.source, self.authority = graph, source, authority
        self.settings = settings or IndexSettings()
        self.readiness, self.repository_lock, self.mode = (
            readiness,
            repository_lock,
            mode,
        )
        self.manifest = compatibility_manifest or confirmed_legacy_manifest()
        self.chunk_verifier, self.store = chunk_verifier, store
        if mode not in {"LIVE", "ARCHIVED"}:
            raise ValueError("Invalid index mode")

    async def refresh(self, baseline, previous=None):
        if self.mode == "LIVE" and (
            self.readiness is None or self.repository_lock is None
        ):
            raise ContextConstructionError(
                "Live derived indexing requires authoritative readiness and a mutation lock"
            )
        if (
            baseline.repository_status != "COMPLETED"
            or baseline.readiness != "READY"
            or baseline.unfinished_ingestion
        ):
            raise ContextConstructionError("Index baseline is not ready")
        lock = (
            self.repository_lock(baseline.repository_id)
            if self.repository_lock
            else asyncio.Lock()
        )
        async with lock:
            before = (
                await self.readiness.snapshot(baseline.repository_id)
                if self.readiness
                else baseline
            )
            if before != baseline:
                raise ContextConstructionError(
                    "Index generation does not match requested baseline"
                )
            deadline = time.monotonic() + self.settings.deadline_seconds
            task = asyncio.create_task(
                asyncio.to_thread(self._materialize, baseline, deadline)
            )
            try:
                data = await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception as exc:  # noqa: BLE001 - retain lease until reader drains
                    logger.debug(
                        "Cancelled index reader ended with error_type=%s",
                        type(exc).__name__,
                    )
                raise
            # No embedding provider is available to refresh: reuse stored chunks only.
            data.update(embedding_requests=0, embeddings_reused=len(data["adr_chunks"]))
            data["index_id"] = fingerprint(
                SearchIndex.model_construct(index_id="", **data).model_dump(
                    mode="json",
                    exclude={"index_id", "embedding_requests", "embeddings_reused"},
                )
            )
            result = SearchIndex.model_validate_json(
                SearchIndex.model_construct(**data).model_dump_json()
            )
            after = (
                await self.readiness.snapshot(baseline.repository_id)
                if self.readiness
                else baseline
            )
            if after != before:
                raise ContextConstructionError(
                    "Baseline changed during derived indexing"
                )
            if self.store:
                task = asyncio.create_task(asyncio.to_thread(self.store.save, result))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            return result

    def _materialize(self, baseline, deadline):
        repo, settings = baseline.repository_id, self.settings
        evidence, verified, notes, units = {}, set(), [], {}
        source = BoundedSources(self.source, repo, settings, deadline)

        def note(code, message, paths=(), ids=()):
            notes.append(
                Limitation(
                    code=code, message=message, paths=tuple(paths), item_ids=tuple(ids)
                )
            )

        def page(reader, limit, channel, *args, **kwargs):
            if time.monotonic() >= deadline:
                note("INDEX_DEADLINE", "Index refresh deadline", ids=(channel,))
                return None
            try:
                result = reader(*args, limit, **kwargs)
            except Exception as exc:  # noqa: BLE001 - metadata failure is incomplete, never absence
                note(channel + "_UNAVAILABLE", type(exc).__name__)
                return None
            if result.truncated:
                note(
                    channel + "_TRUNCATED",
                    "Derived inventory is incomplete beyond its configured read bound",
                )
            return result

        code, adr = original_evidence_resolvers(
            repo,
            ContextSettings(),
            source,
            baseline.indexed_rkg_sha,
            evidence,
            verified,
            note,
            _scope,
        )

        def keep(data):
            data["source_hash"] = fingerprint(data)
            unit = SearchUnit.model_validate_json(json.dumps(data))
            prior = units.get(unit.canonical_id)
            if prior and prior != unit:
                raise ValueError("Conflicting original retrieval identity")
            units[unit.canonical_id] = unit

        catalog, complete, adr_status, community_links = (), False, "DEGRADED", ()
        communities = {}
        with self.graph.read_snapshot(repo) as graph:
            code_page = page(
                graph.code_inventory, settings.max_code_units, "CODE_INDEX"
            )
            if code_page:
                for position, row in enumerate(code_page.rows):
                    if time.monotonic() >= deadline:
                        note(
                            "INDEX_DEADLINE",
                            "Remaining code inventory omitted at the refresh deadline",
                            ids=tuple(
                                r["chunk"]["id"] for r in code_page.rows[position:]
                            ),
                        )
                        break
                    chunk = row["chunk"]
                    _scope(chunk, repo)
                    if row["path"] != chunk.get("filePath") or chunk.get("adr_id"):
                        raise ValueError("Code inventory provenance mismatch")
                    for node in row.get("entities", ()):
                        _scope(node, repo)
                    value = code(chunk)
                    if value is None or value.evidence_id not in verified:
                        note(
                            "CODE_INDEX_SOURCE_REJECTED",
                            "Chunk is not hash/placement verified at indexed baseline",
                            ids=(chunk.get("id", "unknown"),),
                        )
                        continue
                    names = tuple(
                        sorted(
                            {
                                n["name"]
                                for n in row.get("entities", ())
                                if n and n.get("name")
                            }
                        )
                    )
                    tokens_text = (
                        row["path"] + "\n" + " ".join(names) + "\n" + value.excerpt
                    )
                    if len(tokens_text) > settings.max_index_text_chars:
                        note(
                            "INDEX_LEXICAL_TEXT_TRUNCATED",
                            "Code lexical projection bounded",
                            ids=(chunk["id"],),
                        )
                    keep(
                        {
                            "canonical_id": stable_id(repo, "CODE", chunk["id"]),
                            "category": "CODE",
                            "original_id": chunk["id"],
                            "evidence_ids": [value.evidence_id],
                            "entity_ids": sorted(
                                n["id"]
                                for n in row.get("entities", ())
                                if n and n.get("id")
                            ),
                            "entity_names": list(names),
                            "requirement": None,
                            "lexical_tokens": tokenize(
                                tokens_text[: settings.max_index_text_chars]
                            ),
                        }
                    )
            documents = page(graph.adr_catalog, settings.max_documents, "ADR_CATALOG")
            if documents is not None:
                rows = []
                for row in documents.rows:
                    doc = row["adr"]
                    _scope(doc, repo)
                    rows.append(
                        ADRIdentity(adr_id=doc["id"], content_hash=doc["content_hash"])
                    )
                catalog = tuple(sorted(rows, key=lambda a: a.adr_id))
                complete = not documents.truncated
                adr_status = "AVAILABLE" if catalog or not complete else "ABSENT"
            approved = set()
            if catalog and self.authority:
                view = SimpleNamespace(
                    revision=SimpleNamespace(repository_id=repo), adr_snapshot=catalog
                )
                try:
                    receipts = self.authority.receipts(view)
                    approved = {
                        r.adr_id
                        for r in receipts
                        if r.repository_id == repo
                        and r.status == "APPROVED"
                        and any(
                            a.adr_id == r.adr_id and a.content_hash == r.document_hash
                            for a in catalog
                        )
                    }
                except Exception as exc:  # noqa: BLE001 - approval unavailable stays unknown
                    note("ADR_APPROVAL_UNAVAILABLE", type(exc).__name__)
            for doc in catalog:
                if doc.adr_id not in approved:
                    note(
                        "ADR_INDEX_APPROVAL_UNKNOWN",
                        "Original repository-associated completed ADR bytes could not be verified",
                        ids=(doc.adr_id,),
                    )
            if catalog:
                requirements = page(
                    graph.requirements,
                    settings.max_adr_units,
                    "ADR_REQUIREMENTS",
                    (),
                    fallback=True,
                )
                if requirements:
                    scope_ids = set()
                    for position, row in enumerate(requirements.rows):
                        if time.monotonic() >= deadline:
                            note(
                                "INDEX_DEADLINE",
                                "Remaining requirement inventory omitted at the refresh deadline",
                                ids=tuple(
                                    r["knowledge"]["id"]
                                    for r in requirements.rows[position:]
                                ),
                            )
                            break
                        node = row["knowledge"]
                        _scope(node, repo)
                        scope_ids.update(scope_chunk_ids(read_scope(node).metadata))
                    scope_page = (
                        page(
                            graph.applicability_evidence,
                            settings.max_adr_units,
                            "ADR_SCOPE_SUPPORT",
                            tuple(a.adr_id for a in catalog),
                            tuple(sorted(scope_ids)),
                        )
                        if scope_ids
                        else None
                    )
                    extra = list(scope_page.rows) if scope_page else []
                    for position, row in enumerate(requirements.rows):
                        if time.monotonic() >= deadline:
                            note(
                                "INDEX_DEADLINE",
                                "Remaining requirement sources omitted at the refresh deadline",
                                ids=tuple(
                                    r["knowledge"]["id"]
                                    for r in requirements.rows[position:]
                                ),
                            )
                            break
                        node = row["knowledge"]
                        _scope(node, repo)
                        record = read_scope(node)
                        support = list(row.get("support", ()))
                        owners = {s["adr"]["id"] for s in support if s.get("adr")}
                        support += [
                            s
                            for s in extra
                            if s.get("adr")
                            and s["adr"]["id"] in owners
                            and s.get("chunk")
                            and s["chunk"]["id"] in scope_chunk_ids(record.metadata)
                        ]
                        snapshots = {}
                        for item in support:
                            owner = item.get("adr")
                            if not owner or owner.get("id") not in approved:
                                continue
                            if not any(
                                a.adr_id == owner["id"]
                                and a.content_hash == owner.get("content_hash")
                                for a in catalog
                            ):
                                raise ValueError(
                                    "Requirement escaped verified ADR catalog"
                                )
                            value = adr(item)
                            if value:
                                snapshots[value.evidence_id] = value
                        if not snapshots:
                            note(
                                "ADR_INDEX_SOURCE_REJECTED",
                                "Requirement has no verified original ADR support",
                                ids=(node["id"],),
                            )
                            continue
                        for target in row.get("targets", ()):
                            _scope(target, repo)
                        links = tuple(
                            RequirementEntityLink(
                                entity_id=t["id"],
                                name=t["name"],
                                entity_type=t["label"],
                                relationship=t["relationship"],
                                origin="UNKNOWN",
                            )
                            for t in sorted(
                                row.get("targets", ()), key=lambda t: t.get("id", "")
                            )
                            if t.get("id")
                            and t.get("name")
                            and t.get("label")
                            and t.get("relationship") in {"AFFECTS", "CONSTRAINS"}
                        )
                        values = tuple(snapshots[eid] for eid in sorted(snapshots))
                        missing_scope = set(scope_chunk_ids(record.metadata)) - {
                            v.chunk_id for v in values
                        }
                        if missing_scope:
                            note(
                                "INDEX_SCOPE_EVIDENCE_MISSING",
                                "Declared scope support could not be resolved within its original ADRs",
                                ids=(node["id"], *sorted(missing_scope)),
                            )
                        requirement = RequirementCandidate(
                            requirement_id=node["id"],
                            kind=row["kind"],
                            title=node.get("title"),
                            statement=node.get("description") or "",
                            requirement_type=node.get("decision_type")
                            or node.get("constraint_type"),
                            adr_ids=tuple(sorted({v.adr_id for v in values})),
                            target_entity_ids=tuple(
                                sorted({t.entity_id for t in links})
                            ),
                            target_links=links,
                            evidence_ids=tuple(v.evidence_id for v in values),
                            discovery_reasons=("DERIVED_INDEX_ORIGINAL_SUPPORT",),
                            relevance_score=0.0,
                            declared_applicability_json=record.metadata_json,
                            scope_metadata_status=record.status,
                            enrichment_fingerprint=record.fingerprint,
                            enrichment_status=record.enrichment_status,
                            enrichment_model=record.model,
                            scope_evidence_ids=tuple(
                                v.evidence_id
                                for v in values
                                if v.chunk_id in scope_chunk_ids(record.metadata)
                            ),
                        )
                        # Original requirement first; support/qualifier text is retrieval data.
                        qualifiers = record.metadata_json or ""
                        body = "\n".join(
                            [
                                requirement.title or "",
                                requirement.statement,
                                qualifiers,
                                *(v.excerpt for v in values),
                            ]
                        )
                        if len(body) > settings.max_index_text_chars:
                            note(
                                "INDEX_LEXICAL_TEXT_TRUNCATED",
                                "ADR lexical projection bounded; original support retained",
                                ids=(node["id"],),
                            )
                        keep(
                            {
                                "canonical_id": stable_id(repo, "ADR", node["id"]),
                                "category": "ADR",
                                "original_id": node["id"],
                                "evidence_ids": list(requirement.evidence_ids),
                                "entity_ids": list(requirement.target_entity_ids),
                                "entity_names": [t.name for t in links],
                                "requirement": requirement.model_dump(mode="json"),
                                "lexical_tokens": tokenize(
                                    body[: settings.max_index_text_chars]
                                ),
                            }
                        )
            chunks = ()
            if approved:
                reader = getattr(graph, "adr_chunks", None)
                if reader is None:
                    note(
                        "ADR_CHUNK_INDEX_UNAVAILABLE",
                        "Graph adapter has no bounded ADR vector inventory",
                    )
                else:
                    chunk_page = page(
                        reader,
                        settings.max_adr_chunks,
                        "ADR_CHUNK_INDEX",
                        tuple(sorted(approved)),
                    )
                    if chunk_page is not None:
                        verifier = self.chunk_verifier or UploadedADRChunkVerifier(
                            self.authority,
                            settings.max_documents,
                            max(0, settings.max_source_bytes - source.bytes),
                            deadline,
                        )
                        chunks = collect_chunk_vectors(
                            chunk_page.rows,
                            repo=repo,
                            catalog=catalog,
                            approved=approved,
                            units=units,
                            resolve=adr,
                            verifier=verifier,
                            manifest=self.manifest,
                            note=note,
                        )
            if catalog and not chunks:
                note(
                    "ADR_SEMANTIC_INDEX_UNAVAILABLE",
                    "No verified existing ADR chunk vectors; no requirement embeddings generated",
                )
            entity_ids = tuple(
                sorted(
                    {
                        eid
                        for u in units.values()
                        if u.category == "CODE"
                        for eid in u.entity_ids
                    }
                )
            )
            if entity_ids:
                memberships = page(
                    graph.communities,
                    settings.max_code_units,
                    "INDEX_COMMUNITIES",
                    entity_ids,
                )
                if memberships:
                    links = []
                    for row in memberships.rows:
                        _scope(row["entity"], repo)
                        _scope(row["community"], repo)
                        if row["entity"]["id"] not in entity_ids:
                            raise ValueError("Unrequested community membership")
                        links.append(
                            (row["entity"]["id"], str(row["community"]["communityId"]))
                        )
                        community = row["community"]
                        cid = str(community["communityId"])
                        communities[cid] = CommunityIdentity(
                            repository_id=repo,
                            community_id=cid,
                            name=community.get("name"),
                            title=community.get("title"),
                            architectural_role=community.get("architecturalRole"),
                            aliases=tuple(community.get("aliases") or ()),
                        )
                    community_links = tuple(sorted(set(links)))
        used = {eid for u in units.values() for eid in u.evidence_ids}
        used.update(c.evidence_id for c in chunks)
        return {
            "baseline": baseline,
            "vector_manifest": self.manifest,
            "adr_chunks": chunks,
            "catalog": catalog,
            "catalog_hash": fingerprint([a.model_dump(mode="json") for a in catalog]),
            "adr_status": adr_status,
            "catalog_complete": complete,
            "units": tuple(units[k] for k in sorted(units)),
            "evidence": tuple(evidence[k] for k in sorted(used)),
            "community_links": community_links,
            "communities": tuple(communities[cid] for cid in sorted(communities)),
            "limitations": tuple(notes),
            "mode": self.mode,
        }
