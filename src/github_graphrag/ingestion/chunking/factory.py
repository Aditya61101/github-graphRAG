
from .strategy_abc import ChunkerStrategy
from github_graphrag.models.ingestion_plan import ChunkStrategy


class ChunkerFactory:
    """
    Creates the appropriate chunking strategy for a ChunkStrategy.
    """

    _strategies: dict[ChunkStrategy, type[ChunkerStrategy]] = {}

    @classmethod
    def register(
        cls,
        strategy: ChunkStrategy,
        chunker: type[ChunkerStrategy],
    ) -> None:
        cls._strategies[strategy] = chunker

    @classmethod
    def create(
        cls,
        strategy: ChunkStrategy,
    ) -> ChunkerStrategy:
        chunker = cls._strategies.get(strategy)

        if chunker is None:
            raise ValueError(
                f"No chunker registered for strategy: {strategy}"
            )

        return chunker()