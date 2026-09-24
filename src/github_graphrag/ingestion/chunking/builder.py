from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path

from github_graphrag.ingestion.chunking.factory import ChunkerFactory
from github_graphrag.models.ingestion_plan import (
    FilePlan,
    IngestionAction,
    IngestionPlan,
)
from github_graphrag.models.source_chunk import SourceChunk

def build_chunks(
    repo_root: Path,
    plan: IngestionPlan,
    max_workers: int | None = None,
) -> list[SourceChunk]:
    """
    Read all files approved by the ingestion plan and chunk them
    using their configured strategies.
    """

    file_plans = [
        file_plan
        for file_plan in plan.files
        if file_plan.action != IngestionAction.EXCLUDE
    ]

    if not file_plans:
        return []

    if max_workers is None:
        max_workers = min(32, (os.cpu_count() or 1) + 4)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(
            lambda file_plan: _build_file_chunks(
                repo_root,
                file_plan,
            ),
            file_plans,
        )

        chunks: list[SourceChunk] = []

        for file_chunks in results:
            chunks.extend(file_chunks)

    return chunks


def _build_file_chunks(
    repo_root: Path,
    file_plan:FilePlan,
) -> list[SourceChunk]:
    file_path = repo_root / file_plan.path

    if not file_path.is_file():
        raise FileNotFoundError(
            f"Planned file does not exist: {file_plan.path}"
        )

    content = file_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    chunker = ChunkerFactory.create(
        file_plan.chunk_strategy
    )

    return chunker.chunk(
        path=file_plan.path,
        content=content,
        language=file_plan.language,
    )