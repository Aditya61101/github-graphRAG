from __future__ import annotations

from pathlib import PurePosixPath
from ai_services.models.ingestion_plan import ChunkStrategy, FilePlan, IngestionAction

# Language map for tree-sitter symbol chunking
LANGUAGE_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
}

# Ignored extensions that have no architectural relevance
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd",
    ".lock", ".map",
}

# Documentation extensions
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}

# Config extensions
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".ini", ".env.example", ".conf"}


def classify_file_for_ingestion(file_path: str) -> FilePlan:
    """Deterministically assign an IngestionAction and ChunkStrategy to a repository file."""
    path = PurePosixPath(file_path)
    suffix = path.suffix.lower()
    name = path.name.lower()

    # 1. Excluded files
    if suffix in EXCLUDED_EXTENSIONS or name in {"package-lock.json", "uv.lock", "poetry.lock", "yarn.lock"}:
        return FilePlan(
            path=file_path,
            action=IngestionAction.EXCLUDE,
            chunk_strategy=ChunkStrategy.FALLBACK,
            reason="Non-architectural file, binary, image, or lockfile.",
            language=None,
        )

    # 2. Markdown / Docs
    if suffix in MARKDOWN_EXTENSIONS:
        return FilePlan(
            path=file_path,
            action=IngestionAction.INCLUDE,
            chunk_strategy=ChunkStrategy.MARKDOWN_SECTION,
            reason="Documentation containing architectural explanations.",
            language="markdown",
        )

    # 3. Protocol Buffers
    if suffix == ".proto":
        return FilePlan(
            path=file_path,
            action=IngestionAction.INCLUDE,
            chunk_strategy=ChunkStrategy.PROTO_MESSAGE,
            reason="Protobuf service or schema definitions.",
            language="proto",
        )

    # 4. OpenAPI Specs
    if "openapi" in name or "swagger" in name:
        return FilePlan(
            path=file_path,
            action=IngestionAction.INCLUDE,
            chunk_strategy=ChunkStrategy.OPENAPI_OPERATION,
            reason="OpenAPI REST interface specification.",
            language=suffix.lstrip("."),
        )

    # 5. Programming languages with tree-sitter symbols
    if suffix in LANGUAGE_MAP:
        return FilePlan(
            path=file_path,
            action=IngestionAction.INCLUDE,
            chunk_strategy=ChunkStrategy.SYMBOL,
            reason=f"Source code containing architectural symbols in {LANGUAGE_MAP[suffix]}.",
            language=LANGUAGE_MAP[suffix],
        )

    # 6. Configuration / Data formats
    if suffix in CONFIG_EXTENSIONS:
        return FilePlan(
            path=file_path,
            action=IngestionAction.INCLUDE,
            chunk_strategy=ChunkStrategy.CONFIG_SECTION,
            reason="Configuration or deployment specification.",
            language=suffix.lstrip("."),
        )

    # 7. Fallback for other text files
    return FilePlan(
        path=file_path,
        action=IngestionAction.LOW_PRIORITY,
        chunk_strategy=ChunkStrategy.WHOLE_FILE,
        reason="Miscellaneous text file.",
        language=None,
    )
