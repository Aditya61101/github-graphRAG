from pathlib import Path

from chunker import LanguageDetectorImpl

from github_graphrag.ingestion.pipelines import build_pipeline
from github_graphrag.ingestion.splitters.symbol import TreeSitterCodeSplitter
from github_graphrag.ingestion.splitters.markdown import build_markdown_splitter
from github_graphrag.ingestion.splitters.proto import ProtoTextSplitter
from github_graphrag.ingestion.splitters.openapi import OpenAPITextSplitter
from github_graphrag.ingestion.splitters.whole_file import WholeFileSplitter
from github_graphrag.models.ingestion_plan import IngestionPlan

class IngestionRunner:
    def __init__(
        self,
        llm,
        driver,
        embedder,
        database: str,
    ) -> None:
        self.detector = LanguageDetectorImpl()

        self.symbol_splitter = TreeSitterCodeSplitter(
            min_chunk_size=3,
            max_chunk_size=300,
        )

        self.markdown_splitter = build_markdown_splitter()
        self.proto_splitter = ProtoTextSplitter()
        self.openapi_splitter = OpenAPITextSplitter()
        self.whole_file_splitter = WholeFileSplitter()

        self.symbol_pipeline = build_pipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=self.symbol_splitter,
            database=database,
        )

        self.markdown_pipeline = build_pipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=self.markdown_splitter,
            database=database,
        )

        self.proto_pipeline = build_pipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=self.proto_splitter,
            database=database,
        )

        self.openapi_pipeline = build_pipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=self.openapi_splitter,
            database=database,
        )

        self.whole_file_pipeline = build_pipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=self.whole_file_splitter,
            database=database,
        )

    async def ingest_symbol_file(self, path: Path, relative_path: str) -> None:
        language, confidence = self.detector.detect_from_file(path)

        self.symbol_splitter.language = language

        await self.symbol_pipeline.run_async(
            text=path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            document_metadata={
                "path": relative_path,
                "language": language,
                "language_confidence": confidence,
                "chunk_strategy": "symbol",
            },
        )

    async def ingest_markdown_file(self, path: Path, relative_path: str) -> None:
        await self.markdown_pipeline.run_async(
            text=path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            document_metadata={
                "path": relative_path,
                "chunk_strategy": "markdown_section",
            },
        )

    async def ingest_proto_file(self, path: Path, relative_path: str) -> None:
        await self.proto_pipeline.run_async(
            text=path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            document_metadata={
                "path": relative_path,
                "chunk_strategy": "proto_message",
            },
        )

    async def ingest_openapi_file(self, path: Path, relative_path: str) -> None:
        await self.openapi_pipeline.run_async(
            text=path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            document_metadata={
                "path": relative_path,
                "chunk_strategy": "openapi_operation",
            },
        )

    async def ingest_whole_file(self, path: Path, relative_path: str) -> None:
        await self.whole_file_pipeline.run_async(
            text=path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            document_metadata={
                "path": relative_path,
                "chunk_strategy": "whole_file",
            },
        )

    async def ingest_file(
        self,
        path: Path,
        relative_path: str,
        strategy,
    ) -> None:
        strategy_value = (
            strategy.value
            if hasattr(strategy, "value")
            else strategy
        )

        if strategy_value == "symbol":
            await self.ingest_symbol_file(path, relative_path)

        elif strategy_value == "markdown_section":
            await self.ingest_markdown_file(path, relative_path)

        elif strategy_value == "proto_message":
            await self.ingest_proto_file(path, relative_path)

        elif strategy_value == "openapi_operation":
            await self.ingest_openapi_file(path, relative_path)

        elif strategy_value == "whole_file":
            await self.ingest_whole_file(path, relative_path)

        else:
            raise ValueError(
                f"Unsupported strategy: {strategy_value}"
            )

async def ingest_plan(
    repo_root: Path,
    ingestion_plan:IngestionPlan,
    runner: IngestionRunner,
) -> None:
    for file_plan in ingestion_plan.files:

        if file_plan.action == "exclude":
            continue

        path = repo_root / file_plan.path

        if not path.is_file():
            raise FileNotFoundError(
                f"Planned file does not exist: {file_plan.path}"
            )

        await runner.ingest_file(
            path=path,
            relative_path=file_plan.path,
            strategy=file_plan.chunk_strategy,
        )
