from dataclasses import dataclass

@dataclass(frozen=True)
class SourceChunk:
    """
    A semantically meaningful piece of source content that will be
    passed to the GraphRAG ingestion pipeline.
    """

    chunk_id: str
    file_path: str
    content: str
    chunk_index: int
    strategy: str
    
    language: str | None = None
    symbol_name: str | None = None
    symbol_type: str | None = None
    parent_context: str | None = None
    start_line: int | None = None
    end_line: int | None = None