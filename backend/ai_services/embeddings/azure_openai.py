from __future__ import annotations

from typing import Sequence

from openai import AsyncAzureOpenAI


class AzureOpenAIEmbedder:
    """Azure OpenAI embedding provider with internal request batching."""

    def __init__(
        self,
        client: AsyncAzureOpenAI,
        deployment: str,
        dimensions: int,
        batch_size: int = 128,
    ) -> None:
        if not deployment:
            raise ValueError("deployment must not be empty")
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        self.client = client
        self.deployment = deployment
        self._dimensions = dimensions
        self.batch_size = batch_size

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)

        if not texts:
            return []

        if any(not isinstance(text, str) for text in texts):
            raise TypeError("all texts must be strings")

        embeddings: list[list[float]] = []

        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]

            response = await self.client.embeddings.create(
                model=self.deployment,
                input=batch,
            )

            items = sorted(response.data, key=lambda item: item.index)

            if len(items) != len(batch):
                raise RuntimeError(
                    "Azure OpenAI returned a different number of embeddings "
                    f"({len(items)}) than inputs ({len(batch)})"
                )

            for item in items:
                vector = list(item.embedding)

                if len(vector) != self.dimensions:
                    raise RuntimeError(
                        "Azure OpenAI returned an unexpected embedding dimension: "
                        f"expected {self.dimensions}, got {len(vector)}"
                    )

                embeddings.append(vector)

        return embeddings
