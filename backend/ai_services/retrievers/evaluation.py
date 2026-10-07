"""Small offline hook for comparing injected rerankers on labeled chunk sets."""
from dataclasses import dataclass
from math import log2
from time import perf_counter

from .evidence import budget_candidates, deduplicate_candidates, select_evidence
from .reranker import Reranker
from .settings import RetrievalSettings


@dataclass(frozen=True)
class EvidenceEvaluation:
    candidate_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    candidate_recall: float
    evidence_recall: float
    selected_precision: float
    ndcg: float
    reranking_duration_ms: float


def evaluate_reranker(query, candidates, relevant_chunk_ids: set[str],
                      reranker: Reranker, settings: RetrievalSettings) -> EvidenceEvaluation:
    candidates = budget_candidates(deduplicate_candidates(candidates), settings.rerank_max_candidates)
    started = perf_counter()
    ranked = reranker.rerank(query, candidates)
    elapsed = (perf_counter() - started) * 1000
    selected = select_evidence(ranked, candidates, min_score=settings.rerank_min_score,
                               max_evidence=settings.rerank_max_evidence)
    candidate_ids = tuple(candidate.chunk_id for candidate in candidates)
    selected_ids = tuple(item.candidate.chunk_id for item in selected)
    correct = len(set(selected_ids) & relevant_chunk_ids)
    dcg = sum(1 / log2(index + 2) for index, cid in enumerate(selected_ids) if cid in relevant_chunk_ids)
    ideal = sum(1 / log2(index + 2) for index in range(min(len(relevant_chunk_ids), len(selected_ids))))
    return EvidenceEvaluation(
        candidate_ids, selected_ids,
        len(set(candidate_ids) & relevant_chunk_ids) / len(relevant_chunk_ids) if relevant_chunk_ids else 0,
        correct / len(relevant_chunk_ids) if relevant_chunk_ids else 0,
        correct / len(selected_ids) if selected_ids else 0,
        dcg / ideal if ideal else 0, elapsed,
    )
