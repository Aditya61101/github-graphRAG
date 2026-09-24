from github_graphrag.models.ingestion_plan import ChunkStrategy

from .factory import ChunkerFactory
from .strategies.config import ConfigChunker
from .strategies.fallback import FallbackChunker
from .strategies.markdown import MarkdownChunker
from .strategies.openapi import OpenAPIChunker
from .strategies.proto import ProtoChunker
from .strategies.symbol import SymbolChunker
from .strategies.whole_file import WholeFileChunker


def register_chunkers() -> None:
    ChunkerFactory.register(
        ChunkStrategy.SYMBOL,
        SymbolChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.MARKDOWN_SECTION,
        MarkdownChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.OPENAPI_OPERATION,
        OpenAPIChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.PROTO_MESSAGE,
        ProtoChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.CONFIG_SECTION,
        ConfigChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.WHOLE_FILE,
        WholeFileChunker,
    )

    ChunkerFactory.register(
        ChunkStrategy.FALLBACK,
        FallbackChunker,
    )