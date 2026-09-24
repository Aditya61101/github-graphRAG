from abc import ABC, abstractmethod

from github_graphrag.models.source_chunk import SourceChunk

class ChunkerStrategy(ABC):
    """
    Strategy interface for file-specific chunking.
    """

    @abstractmethod
    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        pass