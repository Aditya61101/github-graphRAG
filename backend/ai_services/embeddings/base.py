from __future__ import annotations

from typing import Protocol, Sequence


class Embedder(Protocol):
    """Provider-agnostic asynchronous embedding interface."""

    @property
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this provider."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts while preserving input order."""
        ...
