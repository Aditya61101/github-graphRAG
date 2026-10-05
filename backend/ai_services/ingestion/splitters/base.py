from neo4j_graphrag.components.types import TextChunk, TextChunks


def whole_file_chunks(
    text: str,
    *,
    strategy: str,
    metadata: dict | None = None,
) -> TextChunks:
    chunk_metadata = dict(metadata or {})
    chunk_metadata["chunk_strategy"] = strategy

    return TextChunks(
        chunks=[
            TextChunk(
                text=text,
                index=0,
                metadata=chunk_metadata,
            )
        ]
    )
