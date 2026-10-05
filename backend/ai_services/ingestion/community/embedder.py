"""Community embedding generator utilizing the existing generic Embedder protocol."""

from __future__ import annotations

import hashlib
import logging
from typing import Sequence

from ai_services.embeddings.base import Embedder

from .config import CommunityConfig
from .exceptions import CommunityEmbeddingError
from .models import CommunityEmbedding, CommunitySummary

logger = logging.getLogger(__name__)


def build_embedding_text(
    summary: CommunitySummary,
    include_key_entities: bool = True,
) -> str:
    """Construct deterministic input text for vector embedding."""
    lines = [
        f"Architectural Role: {summary.architectural_role.strip()}",
        f"Summary: {summary.summary.strip()}",
    ]
    if include_key_entities and summary.key_entities:
        lines.append(f"Key Entities: {', '.join(sorted(summary.key_entities))}")
    return "\n".join(lines)


def compute_embedding_version_hash(summary_hash: str, include_key_entities: bool) -> str:
    """Derive an embedding state hash incorporating both the summary hash and embedding configuration."""
    raw = f"{summary_hash}:inkeys={include_key_entities}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CommunityEmbeddingGenerator:
    """Generates vector embeddings for community summaries in batches.

    Depends only on the project's provider-agnostic Embedder interface.
    """

    def __init__(
        self,
        embedder: Embedder,
        config: CommunityConfig | None = None,
    ) -> None:
        self.embedder = embedder
        self.config = config or CommunityConfig()

    @property
    def dimensions(self) -> int:
        """Expected dimensionality from the configured embedder."""
        return self.embedder.dimensions

    def get_version_hash(self, summary_hash: str) -> str:
        """Return the composite embedding version hash for change tracking."""
        return compute_embedding_version_hash(
            summary_hash,
            self.config.include_key_entities_in_embedding,
        )

    async def generate_embeddings(
        self,
        summaries: Sequence[CommunitySummary],
    ) -> list[CommunityEmbedding]:
        """Generate embeddings in batches for the supplied community summaries."""
        summaries_list = list(summaries)
        if not summaries_list:
            return []

        batch_size = max(1, self.config.embedding_batch_size)
        expected_dims = self.dimensions
        results: list[CommunityEmbedding] = []

        logger.info(
            "Generating embeddings for %d communities (batch_size=%d, dimensions=%d)",
            len(summaries_list),
            batch_size,
            expected_dims,
        )

        for start in range(0, len(summaries_list), batch_size):
            batch = summaries_list[start : start + batch_size]
            batch_ids = [s.community_id for s in batch]
            texts = [
                build_embedding_text(
                    s,
                    include_key_entities=self.config.include_key_entities_in_embedding,
                )
                for s in batch
            ]

            try:
                vectors = await self.embedder.embed(texts)
            except Exception as exc:
                raise CommunityEmbeddingError(
                    f"Embedding provider failed during batch embedding: {exc}",
                    community_ids=batch_ids,
                    cause=exc,
                ) from exc

            if len(vectors) != len(batch):
                raise CommunityEmbeddingError(
                    f"Embedder returned {len(vectors)} vectors for {len(batch)} inputs.",
                    community_ids=batch_ids,
                )

            for summary, vector in zip(batch, vectors, strict=True):
                if len(vector) != expected_dims:
                    raise CommunityEmbeddingError(
                        f"Vector dimension mismatch: expected {expected_dims}, got {len(vector)}.",
                        community_ids=[summary.community_id],
                    )
                v_hash = self.get_version_hash(summary.summary_hash)
                results.append(
                    CommunityEmbedding(
                        community_id=summary.community_id,
                        embedding=vector,
                        dimensions=expected_dims,
                        summary_hash=v_hash,
                    )
                )

        return results
