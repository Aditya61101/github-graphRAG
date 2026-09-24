from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk
from proto_schema_parser.parser import Parser

class ProtoChunker(ChunkerStrategy):
    """
    Creates semantic chunks from a .proto file.

    Each top-level message, enum, service, or extend declaration
    becomes a separate chunk.
    """

    _CHUNKABLE_TYPES = {
        "Message",
        "Enum",
        "Service",
        "Extend",
    }

    def __init__(self) -> None:
        self._parser = Parser()

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        if not content.strip():
            return []

        try:
            proto_file = self._parser.parse(content)
        except Exception as exc:
            raise ValueError(
                f"Failed to parse protobuf file '{path}': {exc}"
            ) from exc

        chunks: list[SourceChunk] = []

        for element in proto_file.file_elements:
            element_type = type(element).__name__

            if element_type not in self._CHUNKABLE_TYPES:
                continue

            name = getattr(element, "name", None)

            if not name:
                continue

            chunks.append(
                SourceChunk(
                    chunk_id=f"{path}:{element_type}:{name}",
                    file_path=path,
                    content=self._render_element(element),
                    chunk_index=len(chunks),
                    strategy="proto_message",
                )
            )

        return chunks

    @staticmethod
    def _render_element(element) -> str:
        """
        Convert the parsed protobuf element back into source text.
        """

        from proto_schema_parser.generator import Generator

        return Generator().generate(element)