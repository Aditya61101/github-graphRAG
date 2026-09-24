# ingestion_plan.py

from enum import Enum

from pydantic import BaseModel, Field


class IngestionAction(str, Enum):
    INCLUDE = "include"
    LOW_PRIORITY = "low_priority"
    EXCLUDE = "exclude"


class ChunkStrategy(str, Enum):
    SYMBOL = "symbol"
    MARKDOWN_SECTION = "markdown_section"
    OPENAPI_OPERATION = "openapi_operation"
    PROTO_MESSAGE = "proto_message"
    CONFIG_SECTION = "config_section"
    WHOLE_FILE = "whole_file"
    FALLBACK = "fallback"


class FilePlan(BaseModel):
    path: str = Field(
        description="Exact repository-relative file path from the manifest."
    )

    action: IngestionAction = Field(
        description=(
            "Whether the file should be included, retained as low priority, "
            "or excluded from architectural ingestion."
        )
    )

    chunk_strategy: ChunkStrategy = Field(
        description=(
            "How the file should later be chunked. "
            "Use FALLBACK when no specialized strategy applies."
        )
    )

    reason: str = Field(
        description=(
            "Short explanation of why this file was assigned this action "
            "and chunking strategy."
        )
    )
    
    language: str | None = Field(
        default=None,
        description=(
            "Optional programming language or file type for the file. "
            "This can help inform chunking strategies."
        )
    )


class IngestionPlan(BaseModel):
    files: list[FilePlan] = Field(
        description=(
            "Ingestion decisions for repository files. "
            "Every file in the provided manifest should be represented."
        )
    )