from langchain_text_splitters import MarkdownHeaderTextSplitter

from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk


class MarkdownChunker(ChunkerStrategy):
    """
    Chunks Markdown documents according to their heading hierarchy.
    """

    _headers_to_split_on = [
        ("#", "h1"),
        ("##", "h2"),
        ("###", "h3"),
        ("####", "h4"),
        ("#####", "h5"),
        ("######", "h6"),
    ]

    def __init__(self) -> None:
        self._splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self._headers_to_split_on,
            strip_headers=False,
        )

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        documents = self._splitter.split_text(content)

        return [
            SourceChunk(
                chunk_id=f"{path}:{index}",
                file_path=path,
                content=document.page_content,
                chunk_index=index,
                strategy="markdown_section",
            )
            for index, document in enumerate(documents)
        ]