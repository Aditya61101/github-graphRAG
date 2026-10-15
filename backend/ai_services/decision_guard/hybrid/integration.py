"""Rank-only integration inside the existing builder's locked read worker.

The index is a hint. Current graph rows and original source resolvers supply
every final record. No snapshot refresh (and therefore no nested lock) here.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field

from ..scope import read_scope, scope_chunk_ids
from .chunks import UploadedADRChunkVerifier
from .models import (
    EvidencePlan,
    IndexSettings,
    RetrievalSettings,
    SearchIndex,
    fingerprint,
)
from .retriever import HybridRetriever


@dataclass(frozen=True)
class HybridContextRetrieval:
    index: SearchIndex | None = None
    # Synchronous, bounded load of an already published immutable snapshot.
    index_provider: object | None = None
    authority: object | None = None
    retriever_factory: object = HybridRetriever
    settings: RetrievalSettings = field(default_factory=RetrievalSettings)
    hydration_limits: IndexSettings = field(default_factory=IndexSettings)
    mode: str = "LIVE"
    # Frozen-source adapters are permitted only in explicitly ARCHIVED builds.
    chunk_verifier: object | None = None

    def __post_init__(self):
        if self.mode not in {"LIVE", "ARCHIVED"}:
            raise ValueError("Explicit live or archived retrieval mode required")
        if self.mode == "LIVE" and self.chunk_verifier is not None:
            raise ValueError("Fixture source verification is forbidden in live mode")


def row_manifest(rows):
    return fingerprint(sorted(rows, key=lambda r: r["knowledge"]["id"]))


class BuildRetrieval:
    def __init__(self, config, discovery, note):
        self.config, self.discovery, self.note = config, discovery, note
        self.plan = None
        self.audit = {"mode": config.mode, "status": "FALLBACK"}
        self.index = None
        self.deadline = time.monotonic() + config.settings.deadline_seconds

    def prepare(self, catalog, complete, rows, selected, evidence):
        """Reject stale policy manifests before semantic work or selection."""
        from ..context import RepositoryScopeError

        cfg = self.config
        self.current_policy_manifest = row_manifest(list(rows.values()))
        try:
            raw = (
                cfg.index_provider(self.discovery.baseline)
                if cfg.index_provider
                else cfg.index
            )
            if raw is None:
                raise ValueError("Prepared index unavailable")
            if (
                len(raw.units) > cfg.settings.max_index_units
                or len(raw.adr_chunks) > cfg.settings.max_semantic_chunks
            ):
                raise ValueError("Index record bound")
            payload = raw.model_dump_json()
            if len(payload.encode()) > cfg.settings.max_index_bytes:
                raise ValueError("Index byte bound")
            index = SearchIndex.model_validate_json(payload)
            if index.baseline.repository_id != self.discovery.revision.repository_id:
                raise RepositoryScopeError("Retrieval index escaped repository")
            if index.mode != cfg.mode or index.baseline != self.discovery.baseline:
                raise ValueError("Index mode/generation/base mismatch")
            if (
                cfg.mode == "LIVE"
                and index.vector_manifest.identity.provenance == "MOCK"
            ):
                raise ValueError(
                    "Mock embedding manifests are forbidden in live builds"
                )
            current = sorted((a.adr_id, a.content_hash) for a in catalog)
            if (
                not complete
                or not index.catalog_complete
                or current != sorted((a.adr_id, a.content_hash) for a in index.catalog)
            ):
                raise ValueError("ADR catalog freshness unverified")
            if index.catalog and cfg.authority is None:
                raise ValueError("Original upload approval verifier unavailable")
            pool = {e.evidence_id: e for e in index.evidence}
            # Current requirement inventory need not be complete. Every derived
            # policy must, however, be resolvable here; otherwise degrade safely.
            for unit in index.units:
                req = unit.requirement
                if req is None:
                    continue
                row = rows.get(req.requirement_id)
                if row is None or req.requirement_id not in selected:
                    raise ValueError(
                        "Index policy absent from bounded current inventory"
                    )
                node = row["knowledge"]
                scope = read_scope(node)
                values = {v.evidence_id: v for v in selected[req.requirement_id][3]}
                if (
                    req.kind != row["kind"]
                    or req.statement != (node.get("description") or "")
                    or req.title != node.get("title")
                    or req.requirement_type
                    != (node.get("decision_type") or node.get("constraint_type"))
                    or req.scope_metadata_status != scope.status
                    or req.enrichment_fingerprint != scope.fingerprint
                    or req.declared_applicability_json != scope.metadata_json
                    or req.enrichment_status != scope.enrichment_status
                    or req.enrichment_model != scope.model
                    or set(req.target_entity_ids)
                    != {t["id"] for t in row.get("targets", ()) if t.get("id")}
                    or {
                        (t.entity_id, t.name, t.entity_type, t.relationship)
                        for t in req.target_links
                    }
                    != {
                        (t["id"], t["name"], t["label"], t["relationship"])
                        for t in row.get("targets", ())
                        if t.get("id")
                        and t.get("name")
                        and t.get("label")
                        and t.get("relationship") in {"AFFECTS", "CONSTRAINS"}
                    }
                    or any(pool[eid] != values.get(eid) for eid in req.evidence_ids)
                ):
                    raise ValueError("Index requirement/source/scope manifest changed")
            self.index = index
            retriever = cfg.retriever_factory(index, authority=cfg.authority)
            # This worker owns a private event loop. No graph/source discovery or
            # mutation-lock acquisition occurs in HybridRetriever.
            plan = asyncio.run(retriever.retrieve(self.discovery, cfg.settings))
            encoded = plan.model_dump_json()
            if len(encoded.encode()) > cfg.settings.max_index_bytes:
                raise ValueError("EvidencePlan byte bound")
            plan = EvidencePlan.model_validate_json(encoded)
            if (
                plan.revision != self.discovery.revision
                or plan.baseline != self.discovery.baseline
                or plan.index_id != index.index_id
                or plan.vector_manifest != index.vector_manifest
            ):
                raise ValueError("Plan revision/index/embedding identity mismatch")
            units = {u.canonical_id: u for u in index.units}
            for c in (*plan.ranked_code, *plan.ranked_adr):
                u = units.get(c.canonical_id)
                if u is None and c.category == "CODE":
                    from ..models import stable_id
                    from .lexical import tokenize
                    from .models import SearchUnit

                    sources = {e.evidence_id: e for e in self.discovery.evidence}
                    refs = [sources.get(eid) for eid in c.evidence_ids]
                    if len(refs) == 1 and refs[0] is not None:
                        e = refs[0]
                        if (
                            e.kind == "CODE"
                            and e.origin == "INDEXED_CODE"
                            and e.placement == "EXACT"
                            and e.chunk_id == c.original_id
                        ):
                            data = {
                                "canonical_id": stable_id(
                                    e.repository_id, "CODE", e.chunk_id
                                ),
                                "category": "CODE",
                                "original_id": e.chunk_id,
                                "evidence_ids": [e.evidence_id],
                                "entity_ids": [],
                                "entity_names": [],
                                "requirement": None,
                                "lexical_tokens": tokenize(e.excerpt),
                            }
                            data["source_hash"] = fingerprint(data)
                            u = SearchUnit.model_validate_json(json.dumps(data))
                if u is None or (
                    c.original_id,
                    c.category,
                    c.source_hash,
                    c.evidence_ids,
                ) != (u.original_id, u.category, u.source_hash, u.evidence_ids):
                    raise ValueError("Forged retrieval candidate identity")
            if (
                len(plan.ranked_code) > cfg.settings.max_code_candidates
                or len(plan.ranked_adr) > cfg.settings.max_adr_candidates
                or len(plan.ranked_code) + len(plan.ranked_adr)
                > cfg.settings.max_candidates
            ):
                raise ValueError("EvidencePlan candidate bound")
            originals = {r.relationship_id: r for r in self.discovery.relationships}
            if any(originals.get(r.relationship_id) != r for r in plan.relationships):
                raise ValueError("Retrieval manufactured a relationship")
            original_evidence = {
                e.evidence_id: e for e in (*index.evidence, *self.discovery.evidence)
            }
            if any(original_evidence.get(e.evidence_id) != e for e in plan.evidence):
                raise ValueError("Plan altered original evidence")
            original_requirements = {
                u.original_id: u.requirement for u in index.units if u.requirement
            }
            if any(
                original_requirements.get(r.requirement_id) != r
                for r in plan.requirements
            ):
                raise ValueError("Plan altered original policy")
            # Derived evidence is never copied into final context.
            self.plan = plan
            self.audit.update(
                status="VALIDATED",
                index_id=index.index_id,
                plan_hash=fingerprint(plan.model_dump(mode="json")),
                channels=[c.model_dump(mode="json") for c in plan.channels],
                usage=plan.usage.model_dump(mode="json"),
            )
            for item in plan.limitations:
                if item not in self.discovery.limitations:
                    self.note(item.code, item.message, item.paths, item.item_ids)
            for channel in plan.channels:
                if channel.status in {"UNAVAILABLE", "FAILED", "STALE", "TRUNCATED"}:
                    self.note(
                        "HYBRID_CHANNEL_" + channel.status,
                        channel.channel + ": " + channel.detail,
                    )
            if cfg.mode == "LIVE":
                self.note(
                    "HYBRID_ATOMICITY_UNVERIFIED",
                    "External writers outside the shared repository lock are not atomically fenced",
                )
            return True
        except RepositoryScopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - retrieval infrastructure is optional
            self.note(
                "HYBRID_INDEX_OR_PLAN_REJECTED",
                str(exc)[:400] if isinstance(exc, ValueError) else type(exc).__name__,
            )
            self.audit["failure"] = type(exc).__name__
            self.plan = None
            return False

    def prioritize(self, selected):
        ranks = {c.original_id: c for c in self.plan.ranked_adr}
        result = {}
        for kid, value in selected.items():
            _score, reasons, row, support, communities = value
            c = ranks.get(kid)
            structural = bool(
                set(reasons)
                & {"ENTITY_TARGET", "SCOPE_ENTITY_TARGET", "NEIGHBOR_TARGET"}
            )
            if c is None and not structural:
                self.note(
                    "HYBRID_SELECTION_OMITTED",
                    "Policy not retained by bounded retrieval; applicability remains unassessed",
                    ids=(kid,),
                )
                continue
            required = set(scope_chunk_ids(read_scope(row["knowledge"]).metadata))
            if (
                required - {v.chunk_id for v in support}
                or not support
                or any(v.completeness != "COMPLETE" for v in support)
            ):
                self.note(
                    "HYBRID_HYDRATION_GROUP_REJECTED",
                    "Incomplete original policy support/qualification group",
                    ids=(kid,),
                )
                continue
            # Source-backed association is protected ahead of lexical fan-out.
            # RRF is the only new score; this is discovery, never applicability.
            result[kid] = (
                float(structural) + (c.rrf_score if c else 0),
                reasons + (("HYBRID_RRF",) if c else ()),
                row,
                support,
                communities,
            )
        return result

    def verify_support(self, support):
        verifier = self.config.chunk_verifier
        if verifier is None:
            verifier = getattr(self, "verifier", None)
            if verifier is None:
                limits = self.config.hydration_limits
                self.verifier = verifier = UploadedADRChunkVerifier(
                    self.config.authority,
                    limits.max_documents,
                    limits.max_source_bytes,
                    self.deadline,
                )
        return verifier(support)

    def recheck_uploads(self, catalog):
        from types import SimpleNamespace

        from ..context import ContextConstructionError

        view = SimpleNamespace(
            revision=self.discovery.revision, adr_snapshot=tuple(catalog)
        )
        receipts = self.config.authority.receipts(view)
        approved = {
            (r.adr_id, r.document_hash)
            for r in receipts
            if r.repository_id == self.discovery.revision.repository_id
            and r.status == "APPROVED"
        }
        if {(a.adr_id, a.content_hash) for a in catalog} - approved:
            raise ContextConstructionError(
                "Original ADR upload changed during hydration"
            )

    def hydrate_code(
        self, graph, entities, evidence, verified, resolve, keep, settings
    ):
        from ..context import RepositoryScopeError, _scope
        from ..delta import symbols

        units = {u.original_id: u for u in self.index.units if u.category == "CODE"}
        expected = {
            e.evidence_id: e for e in (*self.index.evidence, *self.discovery.evidence)
        }
        candidates = self.plan.ranked_code
        # Existing structural evidence already passed the exact original resolver
        # in this worker. Reuse it rather than reading graph/Git again.
        pending = []
        for c in candidates:
            if all(
                eid in verified and evidence.get(eid) == expected.get(eid)
                for eid in c.evidence_ids
            ):
                continue
            pending.append(c)
        selected, omitted = (
            pending[: settings.max_neighbor_chunks],
            pending[settings.max_neighbor_chunks :],
        )
        if omitted:
            self.note(
                "HYBRID_CODE_HYDRATION_OMITTED",
                "Ranked original source groups exceeded the existing neighbor-source bound",
                ids=tuple(c.original_id for c in omitted),
            )
        if not selected:
            return
        reader = getattr(graph, "code_chunks", None)
        if reader is None:
            self.note(
                "HYBRID_CODE_HYDRATION_UNAVAILABLE",
                "Scoped original chunk hydration port unavailable",
                ids=tuple(c.original_id for c in selected),
            )
            return
        ids = tuple(c.original_id for c in selected)
        try:
            page = reader(ids, len(ids))
        except Exception as exc:  # noqa: BLE001 - partial infrastructure is reported
            self.note("HYBRID_CODE_HYDRATION_UNAVAILABLE", type(exc).__name__, ids=ids)
            return
        found = set()
        for row in page.rows:
            if time.monotonic() >= self.deadline:
                self.note(
                    "HYBRID_HYDRATION_DEADLINE",
                    "Original code hydration deadline reached",
                    ids=tuple(sorted(set(ids) - found)),
                )
                break
            chunk = row["chunk"]
            _scope(chunk, self.discovery.revision.repository_id)
            cid = chunk.get("id")
            if (
                cid not in ids
                or row.get("path") != chunk.get("filePath")
                or chunk.get("adr_id")
            ):
                raise RepositoryScopeError(
                    "Code hydration escaped authorized candidates"
                )
            found.add(cid)
            unit = units[cid]
            nodes = [n for n in row.get("entities", ()) if n and n.get("id")]
            for node in nodes:
                _scope(node, self.discovery.revision.repository_id)
            value = resolve(chunk)
            if (
                value is None
                or value.evidence_id not in verified
                or expected.get(value.evidence_id) != value
                or set(unit.entity_ids) != {n["id"] for n in nodes}
                or set(unit.entity_names) != {n["name"] for n in nodes}
            ):
                self.note(
                    "HYBRID_CODE_SOURCE_REJECTED",
                    "Current original chunk/source/entity manifest disagrees with index",
                    ids=(cid,),
                )
                continue
            declared = set(
                symbols(chunk.get("text"), 1, len(chunk.get("text", "").splitlines()))
            )
            for node in nodes:
                if node.get("name") not in declared:
                    continue  # References cannot add paths/support to a declaration.
                existing = entities.get(node["id"])
                keep(
                    node,
                    mapping=existing.mapping if existing else "NEIGHBOR",
                    paths=(row["path"],),
                    ev=(value.evidence_id,),
                    distance=existing.distance if existing else 2,
                    code=True,
                    reason="Verified original source selected through hybrid retrieval",
                )
        if set(ids) - found or page.truncated:
            self.note(
                "HYBRID_CODE_SOURCE_MISSING",
                "Current bounded hydration did not return every original candidate",
                ids=tuple(sorted(set(ids) - found)),
            )
