from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk

class WholeFileChunker(ChunkerStrategy):
    """
    Keeps the entire file as a single chunk.
    """

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        if not content.strip():
            return []

        return [
            SourceChunk(
                chunk_id=f"{path}:0",
                file_path=path,
                content=content,
                chunk_index=0,
                strategy="whole_file",
            )
        ]