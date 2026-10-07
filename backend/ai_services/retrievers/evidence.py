"""Chunk candidates and the single selected-evidence output boundary."""
from dataclasses import dataclass, field, replace
import math
from typing import Any

from ai_services.graph.context import GraphContext
from ai_services.graph.identity import assertion_graph_id, edge_graph_id, entity_graph_id


@dataclass(frozen=True)
class DiscoverySignal:
    channel: str
    rank: int
    score: float | None = None
    signal_id: str | None = None


@dataclass(frozen=True)
class SupportedRelationship:
    source_id: str
    relationship: str
    target_id: str
    assertion_id: str | None = None


@dataclass(frozen=True)
class EvidenceCandidate:
    chunk_id: str
    text: str
    file_path: str | None
    repository: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    discovery_signals: tuple[DiscoverySignal, ...] = ()
    entity_ids: tuple[str, ...] = ()
    relationships: tuple[SupportedRelationship, ...] = ()


@dataclass(frozen=True)
class RerankedEvidence:
    candidate: EvidenceCandidate
    rerank_score: float
    rank: int


def deduplicate_candidates(candidates, repository_id=None):
    by_id: dict[str, EvidenceCandidate] = {}
    for candidate in candidates:
        if not candidate.chunk_id or not candidate.text.strip():
            continue
        if repository_id is not None and candidate.repository != repository_id:
            continue
        previous = by_id.get(candidate.chunk_id)
        if previous:
            if previous.repository != candidate.repository or previous.text != candidate.text:
                raise ValueError(f"Conflicting data for evidence chunk {candidate.chunk_id}")
            candidate = replace(
                previous,
                discovery_signals=tuple(dict.fromkeys(previous.discovery_signals + candidate.discovery_signals)),
            )
        by_id[candidate.chunk_id] = candidate
    return list(by_id.values())


def budget_candidates(candidates, limit):
    # Multiple-channel agreement, then best retrieval rank, then stable ID.
    # Avoid comparing similarity scores from different indexes.
    return sorted(candidates, key=lambda c: (
        -len({s.channel for s in c.discovery_signals}),
        min((s.rank for s in c.discovery_signals), default=10**9),
        c.chunk_id,
    ))[:limit]


def select_evidence(reranked, candidates, *, min_score, max_evidence):
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    seen = set()
    accepted = []
    for item in reranked:
        cid = item.candidate.chunk_id
        if cid not in by_id or cid in seen:
            raise ValueError("Reranker returned unknown or duplicate evidence")
        if not math.isfinite(item.rerank_score):
            raise ValueError("Reranker returned a non-finite score")
        seen.add(cid)
        if item.rerank_score >= min_score:
            accepted.append(replace(item, candidate=by_id[cid]))
    if seen != set(by_id):
        raise ValueError("Reranker did not score every candidate")
    ordered = sorted(accepted, key=lambda item: (-item.rerank_score, item.candidate.chunk_id))
    return [replace(item, rank=index) for index, item in enumerate(ordered[:max_evidence], 1)]


def build_sources(selected):
    """API score is reranker relevance; vector scores remain explicitly named."""
    return {"chunks": [
        {
            **item.candidate.metadata,
            "chunk_id": item.candidate.chunk_id,
            "file_path": item.candidate.file_path or "(unknown file)",
            "repository": item.candidate.repository,
            "excerpt": item.candidate.text,
            "score": item.rerank_score,
            "score_type": "reranker_relevance",
            "retrieval_scores": [
                {"channel": signal.channel, "rank": signal.rank, "score": signal.score}
                for signal in item.candidate.discovery_signals
            ],
        }
        for item in selected
    ]}


def build_graph_context(selected):
    nodes, edges, assertions = set(), set(), set()
    for item in selected:
        candidate = item.candidate
        nodes.update(filter(None, (entity_graph_id(eid) for eid in candidate.entity_ids)))
        for relationship in candidate.relationships:
            if relationship.assertion_id:
                assertions.add(assertion_graph_id(relationship.assertion_id))
            edge_id = edge_graph_id(relationship.source_id, relationship.relationship, relationship.target_id)
            if edge_id:
                edges.add(edge_id)
                nodes.add(entity_graph_id(relationship.source_id))
                nodes.add(entity_graph_id(relationship.target_id))
    return GraphContext(sorted(nodes), sorted(edges), sorted(filter(None, assertions)))
