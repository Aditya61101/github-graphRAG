"""Independent, bounded RRF evidence plans. No final context packing or findings."""

import json
import math
import time
from collections import defaultdict

from ..discovery import PreliminaryDiscovery
from ..models import Limitation, stable_id
from ..scope import discovery as scope_discovery
from ..scope import read_scope, supported_communities, words
from .lexical import BM25, tokenize
from .models import (
    ChannelCoverage,
    ChannelRank,
    EvidencePlan,
    RankedCandidate,
    RetrievalSettings,
    RetrievalUsage,
    SearchIndex,
    SearchUnit,
    SemanticChunkHit,
    fingerprint,
)

CHANNELS = (
    "CODE_RKG",
    "CODE_BM25",
    "ADR_ASSOCIATION",
    "ADR_BM25",
    "ADR_SCOPE",
    "ADR_SEMANTIC",
)


def fair_queries(discovery, limit):
    by_file = defaultdict(list)
    seed_paths = {s.seed_id: s.path for s in discovery.seeds}
    priority = {"CODE_LEXICAL": 0, "ADR_LEXICAL": 1, "ADR_SEMANTIC": 2}
    for query in sorted(
        discovery.queries, key=lambda q: (priority[q.channel], q.query_id)
    ):
        for path in sorted({seed_paths[sid] for sid in query.seed_ids}):
            by_file[path].append(query)
    ordered, seen = [], set()
    for i in range(max((len(q) for q in by_file.values()), default=0)):
        for path in sorted(by_file):
            if i < len(by_file[path]) and by_file[path][i].query_id not in seen:
                query = by_file[path][i]
                seen.add(query.query_id)
                ordered.append(query)
    return ordered[:limit], ordered[limit:]


class HybridRetriever:
    def __init__(self, index=None, *, embedder=None, authority=None):
        # An immutable, verified snapshot is loaded once; no live graph traversal.
        self.index, self.embedder, self.authority = index, embedder, authority

    async def retrieve(self, discovery: PreliminaryDiscovery, settings=None):
        discovery = PreliminaryDiscovery.model_validate_json(
            discovery.model_dump_json()
        )
        settings = settings or RetrievalSettings()
        start, notes = time.monotonic(), list(discovery.limitations)
        deadline = start + settings.deadline_seconds
        before_requests = self.embedder.requests if self.embedder else 0
        before_tokens = self.embedder.estimated_tokens if self.embedder else 0
        pool = {e.evidence_id: e for e in discovery.evidence}
        units, hits, states, channel_queries = (
            {},
            defaultdict(list),
            {},
            defaultdict(set),
        )
        index_id, adr_status, index_units = None, "DEGRADED", 0

        def note(code, message, ids=(), paths=()):
            notes.append(
                Limitation(
                    code=code, message=message, paths=tuple(paths), item_ids=tuple(ids)
                )
            )

        index = None
        if self.index:
            try:
                validated = SearchIndex.model_validate_json(
                    self.index.model_dump_json()
                )
                if validated.baseline != discovery.baseline:
                    raise ValueError("Index repository/generation mismatch")
                if (
                    len(validated.units) > settings.max_index_units
                    or len(validated.adr_chunks) > settings.max_semantic_chunks
                    or len(validated.model_dump_json().encode("utf-8"))
                    > settings.max_index_bytes
                ):
                    raise ValueError("Index read volume budget")
                index = validated
            except (ValueError, TypeError):
                note(
                    "RETRIEVAL_INDEX_STALE_OR_UNAVAILABLE",
                    "Index identity, source manifest or read budget failed",
                )
        else:
            note("RETRIEVAL_INDEX_UNAVAILABLE", "No prepared repository-scoped index")
        if index:
            index_id, adr_status, index_units = (
                index.index_id,
                index.adr_status,
                len(index.units),
            )
            notes.extend(index.limitations)
            units = {u.canonical_id: u for u in index.units}
            for e in index.evidence:
                existing = pool.get(e.evidence_id)
                if existing and existing != e:
                    raise ValueError("Conflicting original evidence identity")
                pool[e.evidence_id] = e
            if self.authority and index.catalog:
                from types import SimpleNamespace

                view = SimpleNamespace(
                    revision=discovery.revision,
                    adr_snapshot=index.catalog,
                )
                try:
                    approved = {
                        r.adr_id
                        for r in self.authority.receipts(view)
                        if r.repository_id == discovery.revision.repository_id
                        and r.status == "APPROVED"
                        and any(
                            a.adr_id == r.adr_id and a.content_hash == r.document_hash
                            for a in index.catalog
                        )
                    }
                except Exception:  # noqa: BLE001 - uncertainty preserves code retrieval
                    approved = set()
                rejected = [
                    u
                    for u in units.values()
                    if u.requirement and not set(u.requirement.adr_ids) <= approved
                ]
                for u in rejected:
                    units.pop(u.canonical_id)
                if rejected:
                    adr_status = "DEGRADED"
                    note(
                        "ADR_RETRIEVAL_SOURCE_STALE",
                        "Original completed ADR approval/byte identity changed",
                        [u.original_id for u in rejected],
                    )
        queries, omitted_queries = fair_queries(discovery, settings.max_queries)
        for q in omitted_queries:
            note(
                "RETRIEVAL_QUERY_OMITTED",
                "Query omitted by fair PR request bound",
                (q.query_id, *q.seed_ids),
            )
        entities = {e.entity_id: e for e in discovery.entities}
        relationship_map = {r.relationship_id: r for r in discovery.relationships}
        associated = defaultdict(set)

        def add(channel, rows, seed_ids, query_id=None):
            if time.monotonic() >= deadline:
                states[channel] = "TRUNCATED"
                note(
                    "RETRIEVAL_DEADLINE",
                    "Channel stopped before complete search",
                    (channel,),
                )
                return
            if query_id:
                channel_queries[channel].add(query_id)
            for uid, _score, _hints in rows[settings.results_per_query :]:
                note(
                    "RETRIEVAL_CHANNEL_OMITTED",
                    channel + " exceeded per-query result bound",
                    (uid, *seed_ids),
                )
            for position, (uid, score, hints_) in enumerate(
                rows[: settings.results_per_query], 1
            ):
                hits[uid].append(
                    ChannelRank(
                        channel=channel,
                        position=position,
                        seed_ids=tuple(seed_ids),
                        query_id=query_id,
                        raw_score=score,
                        hints=tuple(hints_),
                    )
                )
            if states.get(channel) not in {"FAILED", "TRUNCATED"}:
                states[channel] = (
                    "READY" if rows or states.get(channel) == "READY" else "EMPTY"
                )

        if settings.structural:
            original_units = {
                u.original_id: u for u in units.values() if u.category == "CODE"
            }
            for seed in discovery.seeds:
                primary = {
                    eid
                    for eid in seed.baseline_entity_ids
                    if entities[eid].mapping in {"CHANGED_SYMBOL", "CHANGED_CHUNK"}
                }
                rows = {}
                for eid in seed.evidence_ids:
                    e = pool[eid]
                    if (
                        e.kind != "CODE"
                        or e.origin != "INDEXED_CODE"
                        or e.placement != "EXACT"
                        or not e.chunk_id
                    ):
                        continue
                    unit = original_units.get(e.chunk_id)
                    if unit is None:
                        data = {
                            "canonical_id": stable_id(
                                discovery.revision.repository_id, "CODE", e.chunk_id
                            ),
                            "category": "CODE",
                            "original_id": e.chunk_id,
                            "evidence_ids": [eid],
                            "entity_ids": [],
                            "entity_names": [],
                            "requirement": None,
                            "lexical_tokens": tokenize(e.excerpt),
                        }
                        data["source_hash"] = fingerprint(data)
                        unit = SearchUnit.model_validate_json(json.dumps(data))
                        units[unit.canonical_id] = unit
                    rows[unit.canonical_id] = (
                        unit.canonical_id,
                        float(1 + 4 * len(primary.intersection(unit.entity_ids))),
                        ("VERIFIED_BASELINE_SOURCE",),
                    )
                    associated[unit.canonical_id].update(
                        rid
                        for rid in seed.relationship_ids
                        if e.evidence_id in relationship_map[rid].evidence_ids
                        or any(
                            pool[ref].chunk_id == e.chunk_id
                            for ref in relationship_map[rid].evidence_ids
                            if pool[ref].kind == "CODE"
                        )
                    )
                add(
                    "CODE_RKG",
                    sorted(rows.values(), key=lambda r: (-r[1], r[0])),
                    (seed.seed_id,),
                )
                neighbors = set(seed.baseline_entity_ids) - primary
                rows = []
                for u in units.values():
                    if u.category != "ADR":
                        continue
                    direct = primary.intersection(u.entity_ids)
                    nearby = neighbors.intersection(u.entity_ids)
                    if direct or nearby:
                        rows.append(
                            (
                                u.canonical_id,
                                float(4 * len(direct) + len(nearby)),
                                ("EXPLICIT_ENTITY_ASSOCIATION",),
                            )
                        )
                add(
                    "ADR_ASSOCIATION",
                    sorted(rows, key=lambda r: (-r[1], r[0])),
                    (seed.seed_id,),
                )
        else:
            states["CODE_RKG"] = states["ADR_ASSOCIATION"] = "DISABLED"
        code_units = tuple(
            u
            for u in units.values()
            if u.category == "CODE"
            and index
            and u.canonical_id in {r.canonical_id for r in index.units}
        )
        adr_units = tuple(u for u in units.values() if u.category == "ADR")
        if settings.lexical and index:
            lexical = {
                "CODE_LEXICAL": (
                    "CODE_BM25",
                    BM25(code_units, settings.bm25_k1, settings.bm25_b),
                ),
                "ADR_LEXICAL": (
                    "ADR_BM25",
                    BM25(adr_units, settings.bm25_k1, settings.bm25_b),
                ),
            }
            for query in queries:
                if query.channel in lexical:
                    channel, engine = lexical[query.channel]
                    if time.monotonic() >= deadline:
                        add(channel, [], query.seed_ids, query.query_id)
                        continue
                    try:
                        rows = [
                            (uid, score, ()) for uid, score in engine.search(query.text)
                        ]
                        add(channel, rows, query.seed_ids, query.query_id)
                    except Exception:  # noqa: BLE001 - one channel does not establish evidence absence
                        states[channel] = "FAILED"
                        note(
                            "LEXICAL_CHANNEL_FAILED",
                            "Repository BM25 search failed",
                            (channel,),
                        )
        else:
            states["CODE_BM25"] = states["ADR_BM25"] = (
                "DISABLED" if not settings.lexical else "UNAVAILABLE"
            )
        if settings.scope and index:
            memberships = defaultdict(set)
            for eid, cid in index.community_links:
                memberships[eid].add(cid)
            community_catalog = {
                c.community_id: {
                    "id": c.community_id,
                    "name": c.name,
                    "title": c.title,
                    "architectural_role": c.architectural_role,
                    "aliases": list(c.aliases),
                }
                for c in index.communities
            }
            for seed in discovery.seeds:
                primary = {
                    eid
                    for eid in seed.baseline_entity_ids
                    if entities[eid].mapping in {"CHANGED_SYMBOL", "CHANGED_CHUNK"}
                }
                nearby = set(seed.baseline_entity_ids) - primary
                token_set = words(
                    " ".join(
                        (
                            *seed.changed_symbols,
                            *seed.identifiers,
                            *seed.literals,
                            seed.path,
                        )
                    )
                )
                communities = {
                    cid for eid in seed.baseline_entity_ids for cid in memberships[eid]
                }
                languages = {
                    "py": "python",
                    "js": "javascript",
                    "ts": "typescript",
                    "java": "java",
                    "go": "go",
                    "rs": "rust",
                }
                rows = []
                for u in adr_units:
                    r = u.requirement
                    scope = read_scope(
                        {
                            "applicability_json": r.declared_applicability_json,
                            "applicability_status": r.enrichment_status,
                            "applicability_model": r.enrichment_model,
                        }
                    )
                    original_chunks = {
                        pool[eid].chunk_id for eid in r.scope_evidence_ids
                    }
                    meta = scope.metadata
                    members = {
                        eid: {
                            "id": eid,
                            "name": entities[eid].name,
                            "label": entities[eid].entity_type,
                            "aliases": list(entities[eid].aliases),
                            "community_ids": sorted(memberships[eid]),
                        }
                        for eid in seed.baseline_entity_ids
                    }
                    supported = (
                        set(
                            supported_communities(
                                meta, members, community_catalog, original_chunks
                            )
                        )
                        & communities
                    )
                    reasons, score = scope_discovery(
                        scope,
                        {"title": r.title, "description": r.statement},
                        [{"id": eid} for eid in r.target_entity_ids],
                        primary,
                        supported,
                        adr_mention=False,
                        tokens=token_set,
                        languages={languages.get(seed.path.rsplit(".", 1)[-1], "")},
                        paths=(seed.path,),
                        nearby_seeds=nearby,
                        changed_literals=words(" ".join(seed.literals)),
                    )
                    rows.append((u.canonical_id, score, reasons))
                add(
                    "ADR_SCOPE",
                    sorted(rows, key=lambda r: (-r[1], r[0])),
                    (seed.seed_id,),
                )
        else:
            states["ADR_SCOPE"] = "DISABLED" if not settings.scope else "UNAVAILABLE"
        semantic_queries = [q for q in queries if q.channel == "ADR_SEMANTIC"]
        semantic_hits = []
        compatible = []
        adr_by_id = {u.original_id: u for u in adr_units}
        if (
            self.embedder
            and index
            and index.vector_manifest.identity == self.embedder.identity
        ):
            approved_ids = {aid for u in adr_units for aid in u.requirement.adr_ids}
            # Original source-only context chunks may have no SUPPORTS target.
            compatible = [c for c in index.adr_chunks if c.adr_id in approved_ids]
        provenance = "UNMEASURED"
        if settings.semantic and compatible and semantic_queries:
            try:
                query_vectors = await self.embedder.embed(
                    [q.text for q in semantic_queries], deadline=deadline
                )
                provenance = self.embedder.identity.provenance
                for query, vector in zip(semantic_queries, query_vectors, strict=True):
                    channel_queries["ADR_SEMANTIC"].add(query.query_id)
                    norm = math.sqrt(sum(x * x for x in vector))
                    rows = []
                    for chunk in compatible:
                        other = chunk.vector.values
                        cosine = sum(
                            a * b for a, b in zip(vector, other, strict=True)
                        ) / (norm * math.sqrt(sum(x * x for x in other)))
                        rows.append((chunk, cosine))
                    rows.sort(key=lambda r: (-r[1], r[0].chunk_id))
                    if time.monotonic() >= deadline:
                        states["ADR_SEMANTIC"] = "TRUNCATED"
                        note(
                            "RETRIEVAL_DEADLINE",
                            "Semantic source projection stopped at deadline",
                            (query.query_id,),
                        )
                        continue
                    for chunk, _score in rows[settings.semantic_chunks_per_query :]:
                        note(
                            "SEMANTIC_CHUNK_OMITTED",
                            "Semantic chunk hit exceeded per-query chunk bound",
                            (chunk.chunk_id, *query.seed_ids),
                        )
                    projected = set()
                    projection_budget = min(
                        settings.semantic_requirements_per_query,
                        settings.results_per_query,
                    )
                    for position, (chunk, cosine) in enumerate(
                        rows[: settings.semantic_chunks_per_query], 1
                    ):
                        mapped = tuple(
                            rid for rid in chunk.requirement_ids if rid in adr_by_id
                        )
                        semantic_hits.append(
                            SemanticChunkHit(
                                chunk_id=chunk.chunk_id,
                                evidence_id=chunk.evidence_id,
                                vector_key=chunk.vector.key,
                                position=position,
                                cosine=cosine,
                                query_id=query.query_id,
                                seed_ids=query.seed_ids,
                                requirement_ids=mapped,
                                metadata_basis=chunk.metadata_basis,
                            )
                        )
                        for rid in mapped:
                            if (
                                rid not in projected
                                and len(projected) >= projection_budget
                            ):
                                note(
                                    "SEMANTIC_REQUIREMENT_OMITTED",
                                    "Chunk SUPPORTS fan-out exceeded per-query requirement bound",
                                    (chunk.chunk_id, rid, *query.seed_ids),
                                )
                                continue
                            projected.add(rid)
                            hits[adr_by_id[rid].canonical_id].append(
                                ChannelRank(
                                    channel="ADR_SEMANTIC",
                                    position=position,
                                    seed_ids=query.seed_ids,
                                    query_id=query.query_id,
                                    raw_score=cosine,
                                    hints=(
                                        "CHUNK_SIMILARITY_ONLY",
                                        "ORIGINAL_SUPPORTS_PROJECTION",
                                    ),
                                    source_chunk_id=chunk.chunk_id,
                                    source_evidence_id=chunk.evidence_id,
                                    source_vector_key=chunk.vector.key,
                                )
                            )
                    states["ADR_SEMANTIC"] = "READY" if rows else "EMPTY"
                supported = {rid for c in compatible for rid in c.requirement_ids}
                missing = sorted(set(adr_by_id) - supported)
                if missing:
                    note(
                        "SEMANTIC_INDEX_PARTIAL",
                        "Requirements lack verified original chunk vectors",
                        missing,
                    )
            except Exception:  # noqa: BLE001 - preserve other channels, bounded provider owns retries
                states["ADR_SEMANTIC"] = "FAILED"
                note(
                    "SEMANTIC_CHANNEL_FAILED",
                    "Embedding approval, availability or query budget failed",
                )
        else:
            states["ADR_SEMANTIC"] = (
                "DISABLED"
                if not settings.semantic
                else "EMPTY"
                if adr_status == "ABSENT"
                else "UNAVAILABLE"
            )
            if settings.semantic and adr_status != "ABSENT":
                note(
                    "SEMANTIC_CHANNEL_UNMEASURED",
                    "No compatible verified ADR chunk vectors/query provider; no query embedded",
                )
        weights = dict(settings.weights)
        ranked = {"CODE": [], "ADR": []}
        for uid, entries in hits.items():
            # One rank vote per channel, independent of the number of similar queries.
            best = {}
            for h in entries:
                if h.channel not in best or h.position < best[h.channel].position:
                    best[h.channel] = h
            score = sum(
                weights.get(channel, 1.0) / (settings.rrf_k + h.position)
                for channel, h in best.items()
            )
            unit = units[uid]
            ranked[unit.category].append((unit, score, entries))
        for values in ranked.values():
            values.sort(key=lambda item: (-item[1], item[0].canonical_id))
        limits = {
            "CODE": settings.max_code_candidates,
            "ADR": settings.max_adr_candidates,
        }
        if ranked["CODE"] and ranked["ADR"]:
            code_reserve = min(
                settings.max_code_candidates,
                len(ranked["CODE"]),
                (settings.max_candidates + 1) // 2,
            )
            limits["ADR"] = min(limits["ADR"], settings.max_candidates - code_reserve)
            limits["CODE"] = min(
                limits["CODE"],
                settings.max_candidates - min(limits["ADR"], len(ranked["ADR"])),
            )
        selected = {"CODE": [], "ADR": []}
        seed_counts, file_counts, duplicate_counts = (
            defaultdict(int),
            defaultdict(int),
            defaultdict(int),
        )
        seed_paths = {s.seed_id: s.path for s in discovery.seeds}
        # Per-seed round-robin selection within category; scores order each queue.
        for category in ("ADR", "CODE"):

            def seed_score(row, sid):
                positions = {}
                for hit in row[2]:
                    if sid in hit.seed_ids:
                        positions[hit.channel] = min(
                            positions.get(hit.channel, hit.position), hit.position
                        )
                return sum(
                    weights.get(channel, 1.0) / (settings.rrf_k + rank)
                    for channel, rank in positions.items()
                )

            queues = {
                s.seed_id: sorted(
                    [
                        r
                        for r in ranked[category]
                        if any(s.seed_id in h.seed_ids for h in r[2])
                    ],
                    key=lambda row: (
                        -seed_score(row, s.seed_id),
                        -row[1],
                        row[0].canonical_id,
                    ),
                )
                for s in discovery.seeds
            }
            # Interleave seeds within each file, then files. A file with many
            # hunks cannot take every initial slot before another file is seen.
            file_queues = defaultdict(list)
            file_seen = defaultdict(set)
            for i in range(max((len(q) for q in queues.values()), default=0)):
                for sid, queue in queues.items():
                    path = seed_paths[sid]
                    if (
                        i < len(queue)
                        and queue[i][0].canonical_id not in file_seen[path]
                    ):
                        file_seen[path].add(queue[i][0].canonical_id)
                        file_queues[path].append(queue[i])
            seen = set()
            for i in range(max((len(q) for q in file_queues.values()), default=0)):
                for path in sorted(file_queues):
                    queue = file_queues[path]
                    if i >= len(queue):
                        continue
                    unit, score, entries = queue[i]
                    if unit.canonical_id in seen:
                        continue
                    seen.add(unit.canonical_id)
                    all_seeds = tuple(
                        sorted({sid_ for h in entries for sid_ in h.seed_ids})
                    )
                    eligible_seeds = [
                        s
                        for s in all_seeds
                        if seed_paths[s] == path
                        and seed_counts[s] < settings.results_per_seed
                    ]
                    if unit.requirement:
                        r = unit.requirement
                        redundancy = fingerprint(
                            [
                                r.adr_ids,
                                r.kind,
                                " ".join(r.statement.casefold().split()),
                                r.declared_applicability_json,
                            ]
                        )
                    else:
                        redundancy = fingerprint(
                            [
                                (
                                    pool[eid].path,
                                    pool[eid].commit_sha,
                                    pool[eid].excerpt_hash,
                                    pool[eid].location.model_dump()
                                    if pool[eid].location
                                    else None,
                                )
                                for eid in unit.evidence_ids
                            ]
                        )
                    if (
                        len(selected[category]) >= limits[category]
                        or sum(map(len, selected.values())) >= settings.max_candidates
                        or not eligible_seeds
                        or file_counts[path] >= settings.results_per_file
                        or duplicate_counts[redundancy] >= settings.max_redundant
                    ):
                        note(
                            "RANKED_CANDIDATE_OMITTED",
                            "Category, PR, seed, file or exact-redundancy selection bound",
                            (unit.canonical_id, *all_seeds),
                        )
                        continue
                    selected[category].append(
                        RankedCandidate(
                            canonical_id=unit.canonical_id,
                            category=category,
                            original_id=unit.original_id,
                            source_hash=unit.source_hash,
                            evidence_ids=unit.evidence_ids,
                            seed_ids=all_seeds,
                            channels=tuple(entries),
                            rrf_score=score,
                            rank=len(selected[category]) + 1,
                            relationship_ids=tuple(
                                sorted(associated[unit.canonical_id])
                            ),
                        )
                    )
                    duplicate_counts[redundancy] += 1
                    # Allocate one selected group to one seed/file. Its complete
                    # query associations remain in the audit; a shared hit must
                    # not consume every other file's independent capacity.
                    owner = min(
                        eligible_seeds,
                        key=lambda s: (
                            seed_counts[s],
                            -seed_score((unit, score, entries), s),
                            s,
                        ),
                    )
                    seed_counts[owner] += 1
                    file_counts[path] += 1
        for category, values in selected.items():
            selected[category] = [
                c.model_copy(update={"rank": position})
                for position, c in enumerate(
                    sorted(values, key=lambda c: (-c.rrf_score, c.canonical_id)),
                    1,
                )
            ]
        chosen_ids = {c.canonical_id for rows in selected.values() for c in rows}
        requirements = tuple(
            units[cid].requirement
            for cid in sorted(chosen_ids)
            if units[cid].requirement
        )
        relationship_ids = {
            rid
            for rows in selected.values()
            for c in rows
            for rid in c.relationship_ids
        }
        relationships = tuple(relationship_map[rid] for rid in sorted(relationship_ids))
        needed = {
            eid for rows in selected.values() for c in rows for eid in c.evidence_ids
        }
        needed.update(eid for r in relationships for eid in r.evidence_ids)
        needed.update(h.evidence_id for h in semantic_hits)
        coverage = []
        for channel in CHANNELS:
            count = len(
                {
                    uid
                    for uid, entries in hits.items()
                    if any(h.channel == channel for h in entries)
                }
            )
            status = states.get(channel, "EMPTY")
            index_gaps = {
                "CODE_BM25": {
                    "CODE_INDEX_UNAVAILABLE",
                    "CODE_INDEX_TRUNCATED",
                    "CODE_INDEX_SOURCE_REJECTED",
                },
                "ADR_ASSOCIATION": {
                    "ADR_CATALOG_UNAVAILABLE",
                    "ADR_CATALOG_TRUNCATED",
                    "ADR_REQUIREMENTS_UNAVAILABLE",
                    "ADR_REQUIREMENTS_TRUNCATED",
                    "ADR_INDEX_APPROVAL_UNKNOWN",
                },
                "ADR_BM25": {
                    "ADR_CATALOG_UNAVAILABLE",
                    "ADR_CATALOG_TRUNCATED",
                    "ADR_REQUIREMENTS_UNAVAILABLE",
                    "ADR_REQUIREMENTS_TRUNCATED",
                    "ADR_INDEX_APPROVAL_UNKNOWN",
                },
                "ADR_SCOPE": {
                    "ADR_CATALOG_UNAVAILABLE",
                    "ADR_CATALOG_TRUNCATED",
                    "ADR_REQUIREMENTS_UNAVAILABLE",
                    "ADR_REQUIREMENTS_TRUNCATED",
                    "ADR_INDEX_APPROVAL_UNKNOWN",
                    "ADR_SCOPE_SUPPORT_UNAVAILABLE",
                    "ADR_SCOPE_SUPPORT_TRUNCATED",
                    "INDEX_SCOPE_EVIDENCE_MISSING",
                },
                "ADR_SEMANTIC": {
                    "SEMANTIC_INDEX_PARTIAL",
                    "ADR_CHUNK_INDEX_UNAVAILABLE",
                    "ADR_CHUNK_INDEX_TRUNCATED",
                    "ADR_CHUNK_VECTOR_REJECTED",
                    "ADR_CHUNK_SOURCE_REJECTED",
                    "SEMANTIC_MAPPING_REQUIREMENT_OMITTED",
                    "SEMANTIC_CHUNK_OMITTED",
                    "SEMANTIC_REQUIREMENT_OMITTED",
                },
            }
            gaps = sorted(
                {l.code for l in notes if l.code in index_gaps.get(channel, set())}
            )
            if gaps and status in {"READY", "EMPTY"}:
                status = "TRUNCATED" if count else "UNAVAILABLE"
            if status in {"READY", "EMPTY"} and any(
                l.code == "RETRIEVAL_CHANNEL_OMITTED" and channel in l.message
                for l in notes
            ):
                status = "TRUNCATED"
            coverage.append(
                ChannelCoverage(
                    channel=channel,
                    status=status,
                    queries=len(channel_queries[channel]),
                    distinct_candidates=count,
                    detail=", ".join(gaps),
                )
            )
        return EvidencePlan(
            revision=discovery.revision,
            vector_manifest=index.vector_manifest if index else None,
            semantic_chunks=tuple(semantic_hits),
            baseline=discovery.baseline,
            index_id=index_id,
            adr_status=adr_status,
            ranked_code=tuple(selected["CODE"]),
            ranked_adr=tuple(selected["ADR"]),
            evidence=tuple(pool[eid] for eid in sorted(needed)),
            requirements=requirements,
            relationships=relationships,
            channels=tuple(coverage),
            limitations=tuple(
                sorted(notes, key=lambda l: (l.code, l.paths, l.item_ids, l.message))
            ),
            usage=RetrievalUsage(
                embedding_requests=(self.embedder.requests - before_requests)
                if self.embedder
                else 0,
                embedding_estimated_tokens=(
                    self.embedder.estimated_tokens - before_tokens
                )
                if self.embedder
                else 0,
                query_count=len(queries),
                index_units_read=index_units,
                latency_seconds=time.monotonic() - start,
                semantic_provenance=provenance,
            ),
        )
