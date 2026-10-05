from chunker import chunk_text
from neo4j_graphrag.components.types import TextChunk, TextChunks


def split_symbol(
    text: str,
    language: str,
    metadata: dict | None = None,
) -> TextChunks:
    chunks = chunk_text(text, language)

    base_metadata = dict(metadata or {})
    base_metadata.update(
        {
            "chunk_strategy": "symbol",
            "language": language,
        }
    )

    return TextChunks(
        chunks=[
            TextChunk(
                text=chunk.content,
                index=index,
                metadata={
                    **base_metadata,
                    "symbol_type": chunk.node_type,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "parent_context": chunk.parent_context,
                },
            )
            for index, chunk in enumerate(chunks)
        ]
    )
