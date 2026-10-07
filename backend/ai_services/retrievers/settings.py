"""Environment-backed retrieval settings, shared by startup and CLI runners."""
from dataclasses import dataclass
import math
import os


@dataclass(frozen=True)
class RetrievalSettings:
    direct_chunk_top_k: int = 30
    entity_top_k: int = 5
    community_top_k: int = 3
    rerank_max_candidates: int = 60
    rerank_max_evidence: int = 5
    rerank_min_score: float = 0.5
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_device: str = "auto"
    reranker_batch_size: int = 2
    reranker_max_length: int = 4096

    def __post_init__(self):
        for name in (
            "direct_chunk_top_k", "entity_top_k", "community_top_k",
            "rerank_max_candidates", "rerank_max_evidence",
            "reranker_batch_size", "reranker_max_length",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not math.isfinite(self.rerank_min_score):
            raise ValueError("rerank_min_score must be finite")
        if self.reranker_max_length < 128:
            raise ValueError("reranker_max_length must be at least 128")
        if not self.reranker_model.strip():
            raise ValueError("reranker_model must not be empty")

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            raw = os.getenv(name.upper())
            values[name] = type(default)(raw) if raw is not None else default
        return cls(**values)
