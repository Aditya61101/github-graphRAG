from __future__ import annotations

import asyncio
from typing import Sequence

from sentence_transformers import SentenceTransformer


class SentenceTransformerEmbedder:
    """Local SentenceTransformer provider with internal batching."""

    def __init__(
        self,
        model_name: str,
        batch_size: int = 32,
    ) -> None:
        if not model_name:
            raise ValueError("model_name must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        self.model = SentenceTransformer(model_name)
        self.batch_size = batch_size

        dimensions = self.model.get_sentence_embedding_dimension()
        if dimensions is None or dimensions <= 0:
            raise RuntimeError(
                "Could not determine the SentenceTransformer embedding dimension"
            )

        self._dimensions = int(dimensions)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)

        if not texts:
            return []

        if any(not isinstance(text, str) for text in texts):
            raise TypeError("all texts must be strings")

        vectors: list[list[float]] = []

        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]

            encoded = await asyncio.to_thread(
                self.model.encode,
                batch,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

            batch_vectors = encoded.tolist()

            if len(batch_vectors) != len(batch):
                raise RuntimeError(
                    "SentenceTransformer returned a different number of embeddings "
                    f"({len(batch_vectors)}) than inputs ({len(batch)})"
                )

            for vector in batch_vectors:
                if len(vector) != self.dimensions:
                    raise RuntimeError(
                        "SentenceTransformer returned an unexpected embedding "
                        f"dimension: expected {self.dimensions}, got {len(vector)}"
                    )
                vectors.append(vector)

        return vectors
