from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk


class ConfigChunker(ChunkerStrategy):

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        
        if not content.strip():
            return []

        return [SourceChunk(
            chunk_id=f"{path}:0",
            file_path=path,
            content=content,
            chunk_index=0,
            strategy="config",
        )]