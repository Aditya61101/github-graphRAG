from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class ADRSection(BaseModel):
    """Represents a structured section within an Architecture Decision Record."""

    heading: str = Field(description="Heading title, e.g. 'Context', 'Decision', 'Consequences'.")
    level: int = Field(default=1, description="Heading level (1 for H1, 2 for H2, etc.).")
    content: str = Field(description="Body text content under this heading.")


class ADRDocument(BaseModel):
    """Normalized internal representation of an Architecture Decision Record (ADR).

    Consumed by downstream knowledge graph ingestion (chunking, embeddings, LLM extraction, Neo4j).
    """

    adr_id: str = Field(description="Unique identifier for the ADR.")
    repository_id: str = Field(description="Canonical repository identity.")
    title: str = Field(description="Extracted or explicitly provided title of the ADR.")
    content: str = Field(description="Full normalized text content of the ADR.")

    # Source metadata (agnostic to MANUAL_UPLOAD, CONFLUENCE, etc.)
    source_type: str = Field(default="MANUAL_UPLOAD", description="Origin source type.")
    source_name: str = Field(description="Original document or page name.")
    source_id: str | None = Field(default=None, description="External source ID if available.")
    source_url: str | None = Field(default=None, description="External source URL if available.")
    source_version: str | None = Field(default=None, description="Source version or revision.")

    # File and content metadata
    file_path: str = Field(description="Local filesystem path where the raw file is stored.")
    content_hash: str = Field(description="SHA-256 hash of the uploaded document.")
    file_size: int | None = Field(default=None, description="Size of the raw document in bytes.")
    mime_type: str | None = Field(default=None, description="MIME type of the document.")
    file_extension: str | None = Field(default=None, description="File extension with leading dot.")

    # Extracted document structure
    sections: list[ADRSection] = Field(
        default_factory=list,
        description="Structured sections extracted from headings in the document.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional arbitrary metadata extracted during parsing.",
    )
