from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import logging
from time import perf_counter

from neo4j_graphrag.exceptions import SearchValidationError

from ai_services.graph.context import GraphContext
from .context_formatter import format_retrieval_context
from .evidence import (
    DiscoverySignal, EvidenceCandidate, RerankedEvidence, SupportedRelationship,
    budget_candidates, build_graph_context, build_sources, deduplicate_candidates, select_evidence,
)
from .graph_expansion import load_chunk_graph_associations, load_signal_evidence_chunks
from .reranker import Reranker
from .settings import RetrievalSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridRetrievalResult:
    context: str
    sources: dict[str, list[dict]]
    graph_context: GraphContext = field(default_factory=GraphContext)
    selected_evidence: tuple[RerankedEvidence, ...] = ()


async def _search(retriever, vector, top_k, repository_id):
    if retriever is None:
        return []
    kwargs = {"query_vector": vector, "top_k": top_k}
    if repository_id is not None:
        kwargs["filters"] = {"repository": repository_id}
    try:
        result = await asyncio.to_thread(retriever.search, **kwargs)
    except SearchValidationError:
        # Legacy indexes can lack filter metadata. Application filtering still
        # requires an explicit matching repository on every returned hit.
        if repository_id is None:
            raise
        logger.debug("Vector search filter unavailable; applying strict hit filtering")
        result = await asyncio.to_thread(retriever.search, query_vector=vector, top_k=top_k)
    return [
        item for item in result.items
        if repository_id is None or item.metadata.get("repository") == repository_id
    ]


def _hits(items, key):
    return [
        {"id": item.metadata[key], "rank": rank, "score": item.metadata.get("score")}
        for rank, item in enumerate(items, 1) if item.metadata.get(key)
    ]


async def hybrid_retrieve(
    query: str, driver, database: str | None, embedder, entity_retriever,
    community_retriever, reranker: Reranker, chunk_retriever=None,
    repository_id: str | None = None, settings: RetrievalSettings | None = None,
) -> HybridRetrievalResult:
    settings = settings or RetrievalSettings.from_env()
    vectors = await embedder.embed([query])
    if len(vectors) != 1:
        raise RuntimeError("Embedder must return exactly one vector for a query.")

    direct, entities, communities = await asyncio.gather(
        _search(chunk_retriever, vectors[0], settings.direct_chunk_top_k, repository_id),
        _search(entity_retriever, vectors[0], settings.entity_top_k, repository_id),
        _search(community_retriever, vectors[0], settings.community_top_k, repository_id),
    )
    candidates = [
        EvidenceCandidate(
            chunk_id=item.metadata.get("chunk_id") or "", text=item.content,
            file_path=item.metadata.get("file_path"), repository=item.metadata.get("repository"),
            metadata={key: value for key, value in item.metadata.items() if key != "score"},
            discovery_signals=(DiscoverySignal("direct_chunk", rank, item.metadata.get("score")),),
        )
        for rank, item in enumerate(direct, 1)
    ]
    discovered = await asyncio.to_thread(
        load_signal_evidence_chunks, driver, _hits(entities, "canonical_id"),
        _hits(communities, "community_id"), database, repository_id,
        settings.rerank_max_candidates,
    )
    for record in discovered:
        candidates.append(EvidenceCandidate(
            chunk_id=record["chunk_id"], text=record.get("text") or "",
            file_path=record.get("file_path"), repository=record.get("repository"),
            metadata={key: value for key, value in record.items() if key not in {"text", "signals"}},
            discovery_signals=tuple(
                DiscoverySignal(signal["channel"], signal["rank"], signal.get("score"), signal.get("id"))
                for signal in record.get("signals", [])
            ),
        ))
    unique = deduplicate_candidates(candidates, repository_id)
    budgeted = budget_candidates(unique, settings.rerank_max_candidates)
    started = perf_counter()
    reranked = await asyncio.to_thread(reranker.rerank, query, budgeted) if budgeted else []
    selected = select_evidence(
        reranked, budgeted, min_score=settings.rerank_min_score,
        max_evidence=settings.rerank_max_evidence,
    )
    duration_ms = (perf_counter() - started) * 1000

    # Only selected chunk IDs cross the provenance/output boundary.
    associations = await asyncio.to_thread(
        load_chunk_graph_associations, driver, [item.candidate.chunk_id for item in selected],
        database, repository_id,
    )
    by_id = {
        row["chunk_id"]: row for row in associations
        if repository_id is None or row.get("repository") == repository_id
    }
    enriched = []
    for item in selected:
        association = by_id.get(item.candidate.chunk_id, {})
        relationships = tuple(SupportedRelationship(
            record.get("source_id") or "", record.get("relationship") or "",
            record.get("target_id") or "", record.get("assertion_id"),
        ) for record in association.get("assertion_records", []) + association.get("direct_records", []))
        enriched.append(replace(item, candidate=replace(
            item.candidate, entity_ids=tuple(association.get("entity_ids", [])),
            relationships=relationships,
        )))
    selected = tuple(enriched)
    logger.info(
        "retrieval direct_chunk_count=%d entity_result_count=%d community_result_count=%d "
        "candidate_chunk_count=%d deduplicated_candidate_count=%d budgeted_count=%d "
        "reranked_count=%d selected_evidence_count=%d reranking_duration_ms=%.1f",
        len(direct), len(entities), len(communities), len(candidates), len(unique),
        len(budgeted), len(reranked), len(selected), duration_ms,
    )
    logger.debug("selected evidence=%s", [
        (item.candidate.chunk_id, item.candidate.file_path, item.rerank_score) for item in selected
    ])
    return HybridRetrievalResult(
        context=format_retrieval_context(selected), sources=build_sources(selected),
        graph_context=build_graph_context(selected), selected_evidence=selected,
    )
