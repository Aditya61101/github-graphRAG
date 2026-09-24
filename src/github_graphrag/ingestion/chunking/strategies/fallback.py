import re
from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk

class FallbackChunker(ChunkerStrategy):
    """
    Generic fallback for files that don't have a specialized chunking
    strategy.

    Splits content on blank-line boundaries while preserving the
    original paragraph content.
    """

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        if not content.strip():
            return []

        sections = re.split(
            r"\n\s*\n",
            content.strip(),
        )

        chunks: list[SourceChunk] = []

        for index, section in enumerate(sections):
            section = section.strip()

            if not section:
                continue

            chunks.append(
                SourceChunk(
                    chunk_id=f"{path}:{index}",
                    file_path=path,
                    content=section,
                    chunk_index=index,
                    strategy="fallback",
                )
            )

        return chunks