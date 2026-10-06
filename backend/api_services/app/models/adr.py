from __future__ import annotations

from pydantic import BaseModel, Field


class ADRResponse(BaseModel):
    """API response model for an Architecture Decision Record (ADR)."""

    id: str = Field(description="Unique identifier of the ADR record.")
    repository_id: str = Field(description="Associated repository ID.")
    title: str = Field(description="Title of the ADR.")
    description: str | None = Field(default=None, description="Optional brief description.")
    status: str = Field(description="Processing status: PENDING, PROCESSING, COMPLETED, FAILED.")
    source_type: str = Field(description="Source type, e.g. MANUAL_UPLOAD or CONFLUENCE.")
    source_name: str = Field(description="Original uploaded document name.")
    content_hash: str = Field(description="SHA-256 content hash of the document.")
    file_path: str | None = Field(default=None, description="Local disk storage path of the ADR file.")
    file_size: int | None = Field(default=None, description="Size in bytes.")
    file_extension: str | None = Field(default=None, description="File extension.")
    mime_type: str | None = Field(default=None, description="MIME type.")
    error: str | None = Field(default=None, description="Error message if processing failed.")
    created_at: str | None = Field(default=None, description="ISO timestamp of creation.")
    updated_at: str | None = Field(default=None, description="ISO timestamp of last update.")
