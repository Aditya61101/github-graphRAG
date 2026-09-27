from neo4j_graphrag.components.text_splitters.base import TextSplitter
from neo4j_graphrag.components.types import TextChunk, TextChunks


class WholeFileSplitter(TextSplitter):

    async def run(self, text: str) -> TextChunks:
        return TextChunks(
            chunks=[
                TextChunk(
                    text=text,
                    index=0,
                )
            ]
        )
