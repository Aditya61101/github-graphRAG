from collections.abc import Iterator

from neo4j_graphrag.components.text_splitters.base import TextSplitter
from neo4j_graphrag.components.types import TextChunk, TextChunks


class OpenAPITextSplitter(TextSplitter):
    """
    Temporary integration implementation.

    Replace the internals with OpenAPI path/operation-aware splitting.
    """

    async def run(self, text: str) -> TextChunks:
        return TextChunks(
            chunks=[
                TextChunk(
                    text=text,
                    index=0,
                )
            ]
        )
