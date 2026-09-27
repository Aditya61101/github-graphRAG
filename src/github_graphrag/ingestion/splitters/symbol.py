from chunker import chunk_text

from neo4j_graphrag.components.text_splitters.base import TextSplitter
from neo4j_graphrag.components.types import TextChunk, TextChunks


class TreeSitterCodeSplitter(TextSplitter):
    """
    Neo4j TextSplitter backed by treesitter-chunker.

    The language is changed by the ingestion loop before each document
    is passed to SimpleKGPipeline.
    """

    def __init__(
        self,
        language: str | None = None,
        *,
        min_chunk_size: int = 3,
        max_chunk_size: int = 300,
    ) -> None:
        self.language = language
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size

    async def run(self, text: str) -> TextChunks:
        if not self.language:
            raise ValueError(
                "TreeSitterCodeSplitter.language must be set "
                "before splitting."
            )

        chunks = chunk_text(
            text,
            self.language,
            min_chunk_size=self.min_chunk_size,
            max_chunk_size=self.max_chunk_size,
        )

        return TextChunks(
            chunks=[
                TextChunk(
                    text=chunk.content,
                    index=index,
                    metadata={
                        "chunk_strategy": "symbol",
                        "language": self.language,
                        "symbol_type": chunk.node_type,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "parent_context": chunk.parent_context,
                    },
                )
                for index, chunk in enumerate(chunks)
            ]
        )
