from chunker import chunk_text
from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk

class SymbolChunker(ChunkerStrategy):
    """
    Produces AST-aware semantic chunks from source code using
    treesitter-chunker.
    """

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:

        if not content.strip():
            return []

        if not language:
            raise ValueError(
                f"Language is required for symbol chunking: {path}"
            )

        try:
            code_chunks = chunk_text(
                content,
                language,
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to chunk '{path}' "
                f"using language '{language}': {exc}"
            ) from exc

        return [
            self._to_source_chunk(
                path=path,
                code_chunk=code_chunk,
                index=index,
                language=language,
            )
            for index, code_chunk in enumerate(code_chunks)
        ]

    @staticmethod
    def _to_source_chunk(
        path: str,
        code_chunk,
        index: int,
        language: str,
    ) -> SourceChunk:

        # parent_route = getattr(
        #     code_chunk,
        #     "parent_route",
        #     None,
        # )

        return SourceChunk(
            chunk_id=code_chunk.chunk_id,
            file_path=path,
            content=code_chunk.content,
            chunk_index=index,
            strategy="symbol",

            language=language,

            symbol_name=getattr(
                code_chunk,
                "symbol_name",
                None,
            ),
            symbol_type=getattr(
                code_chunk,
                "node_type",
                None,
            ),
            parent_context=getattr(
                code_chunk,
                "parent_context",
                None,
            ),
            # parent_route=(
            #     tuple(parent_route)
            #     if parent_route
            #     else None
            # ),

            start_line=getattr(
                code_chunk,
                "start_line",
                None,
            ),
            end_line=getattr(
                code_chunk,
                "end_line",
                None,
            ),
            # byte_start=getattr(
            #     code_chunk,
            #     "byte_start",
            #     None,
            # ),
            # byte_end=getattr(
            #     code_chunk,
            #     "byte_end",
            #     None,
            # ),

            # node_id=getattr(
            #     code_chunk,
            #     "node_id",
            #     None,
            # ),
            # file_id=getattr(
            #     code_chunk,
            #     "file_id",
            #     None,
            # ),
            # symbol_id=getattr(
            #     code_chunk,
            #     "symbol_id",
            #     None,
            # ),
        )