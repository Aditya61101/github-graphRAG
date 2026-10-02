from neo4j_graphrag.components.text_splitters.base import TextSplitter
from neo4j_graphrag.components.types import TextChunks

from github_graphrag.models.ingestion_plan import ChunkStrategy
from .markdown import split_markdown
from .openapi import split_openapi
from .proto import split_proto
from .symbol import split_symbol
from .whole_file import split_whole_file


class RepositoryTextSplitter(TextSplitter):
    """Dispatch files to the splitter selected by the ingestion plan.

    This object is intentionally mutable: configure() is called immediately
    before each sequential SimpleKGPipeline.run_async() invocation.
    Do not share this instance across concurrent ingestion tasks.
    """

    def __init__(self) -> None:
        self.chunk_strategy: ChunkStrategy | None = None
        self.language: str | None = None

    def configure(
        self,
        strategy: ChunkStrategy,
        language: str | None = None,
    ) -> None:
        self.chunk_strategy = strategy
        self.language = language

    async def run(self, text: str) -> TextChunks:
        if self.chunk_strategy is None:
            raise ValueError("RepositoryTextSplitter is not configured.")

        return self.split(
            text=text,
            strategy=self.chunk_strategy,
            language=self.language,
        )

    def split(
        self,
        *,
        text: str,
        strategy: ChunkStrategy,
        language: str | None = None,
    ) -> TextChunks:
        if strategy == ChunkStrategy.SYMBOL:
            if not language:
                raise ValueError(
                    "language is required for the SYMBOL chunk strategy."
                )
            return split_symbol(text, language)

        if strategy == ChunkStrategy.MARKDOWN_SECTION:
            return split_markdown(text)

        if strategy == ChunkStrategy.OPENAPI_OPERATION:
            return split_openapi(text)

        if strategy == ChunkStrategy.PROTO_MESSAGE:
            return split_proto(text)

        if strategy in {
            ChunkStrategy.WHOLE_FILE,
            ChunkStrategy.FALLBACK,
            ChunkStrategy.CONFIG_SECTION,
        }:
            return split_whole_file(text)

        raise ValueError(f"Unsupported chunk strategy: {strategy}")
