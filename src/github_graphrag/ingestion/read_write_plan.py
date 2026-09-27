import json
from pathlib import Path
from collections import Counter
from itertools import groupby

from github_graphrag.models.ingestion_plan import IngestionPlan
from github_graphrag.models.source_chunk import SourceChunk

PLAN_FILE = Path("ingestion_plan.json")
CHUNK_REPORT_FILE = Path("chunk_report.txt")

# --- Write ---
def save_plan(plan: IngestionPlan, path: Path = PLAN_FILE) -> None:
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    
def save_manifest(manifest: dict, path: Path = Path("repository_manifest.json")) -> None:
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

# --- Read ---
def load_plan(path: Path = PLAN_FILE) -> IngestionPlan:
    return IngestionPlan.model_validate_json(path.read_text(encoding="utf-8"))


def save_chunk_report(
    chunks: list[SourceChunk],
    path: Path = CHUNK_REPORT_FILE,
) -> None:
    strategy_counts = Counter(
        chunk.strategy
        for chunk in chunks
    )

    sorted_chunks = sorted(
        chunks,
        key=lambda chunk: (
            chunk.file_path,
            chunk.chunk_index,
        ),
    )

    lines: list[str] = []

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    lines.append("=" * 100)
    lines.append("CHUNKING REPORT")
    lines.append("=" * 100)
    lines.append("")
    lines.append(f"Total chunks: {len(chunks)}")
    lines.append("")

    lines.append("Chunks by strategy:")
    for strategy, count in sorted(strategy_counts.items()):
        lines.append(f"  {strategy}: {count}")

    lines.append("")
    lines.append("=" * 100)
    lines.append("CHUNKS BY FILE")
    lines.append("=" * 100)

    # ------------------------------------------------------------------
    # Per-file chunks
    # ------------------------------------------------------------------

    for file_path, file_chunks in groupby(
        sorted_chunks,
        key=lambda chunk: chunk.file_path,
    ):
        file_chunks = list(file_chunks)

        lines.append("")
        lines.append("-" * 100)
        lines.append(
            f"{file_path} ({len(file_chunks)} chunks)"
        )
        lines.append("-" * 100)

        for chunk in file_chunks:
            lines.append("")
            lines.append(
                f"[Chunk {chunk.chunk_index}]"
            )
            lines.append(
                f"  ID:       {chunk.chunk_id}"
            )
            lines.append(
                f"  Strategy: {chunk.strategy}"
            )
            lines.append(
                f"  Language: {chunk.language}"
            )

            if chunk.symbol_name is not None:
                lines.append(
                    f"  Symbol:   {chunk.symbol_name}"
                )

            if chunk.symbol_type is not None:
                lines.append(
                    f"  Type:     {chunk.symbol_type}"
                )

            if chunk.parent_context is not None:
                lines.append(
                    f"  Parent:   {chunk.parent_context}"
                )

            if chunk.start_line is not None:
                lines.append(
                    f"  Lines:    "
                    f"{chunk.start_line}-{chunk.end_line}"
                )

            lines.append("")
            lines.append("  Content:")
            lines.append("  " + "\n  ".join(
                chunk.content.splitlines()
            ))

    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )