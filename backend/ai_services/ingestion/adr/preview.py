"""Validated preview artifacts and source fingerprints for applying reviewed results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ai_services.ingestion.adr.applicability import (
    ApplicabilityMetadata,
    ApplicabilityModel,
)
from ai_services.ingestion.adr.applicability_store import ADRRequirement
from ai_services.ingestion.adr.models import ADRChunk
from ai_services.models.adr_document import ADRDocument

PREVIEW_VERSION = "adr-applicability-preview-v1"


def document_fingerprint(doc: ADRDocument, chunks: Sequence[ADRChunk]) -> str:
    """Hash provenance, navigation, and actual chunk content, independent of embeddings."""
    payload = {
        "document": doc.model_dump(
            exclude={"file_path", "file_size", "mime_type", "file_extension"}
        ),
        "chunks": [
            {
                "id": c.chunk_id,
                "section": c.section,
                "index": c.chunk_index,
                "text": c.text,
                "hash": c.content_hash,
                "metadata": c.metadata,
            }
            for c in sorted(chunks, key=lambda c: c.chunk_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class PreviewSource(ApplicabilityModel):
    repository_id: str
    adr_id: str
    kind: Literal["decision", "constraint"]
    title: str
    description: str
    adr_hash: str
    evidence_chunk_ids: list[str]
    document_fingerprint: str
    previous_applicability_json: str | None

    @classmethod
    def capture(
        cls,
        repository_id: str,
        requirement: ADRRequirement,
        doc: ADRDocument,
        chunks: Sequence[ADRChunk],
    ) -> PreviewSource:
        return cls(
            repository_id=repository_id,
            adr_id=requirement.adr_id,
            kind=requirement.kind,
            title=requirement.title,
            description=requirement.description,
            adr_hash=requirement.adr_hash or "",
            evidence_chunk_ids=sorted(requirement.evidence_chunk_ids),
            document_fingerprint=document_fingerprint(doc, chunks),
            previous_applicability_json=requirement.properties.get(
                "applicability_json"
            ),
        )


class PreviewEntry(ApplicabilityModel):
    requirement_id: str
    metadata: ApplicabilityMetadata
    # Legacy reports have no source snapshot; never pretend one existed at preview time.
    source: PreviewSource | None = None


class SavedPreviewReport(ApplicabilityModel):
    planned: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    llm_calls: int = 0
    errors: list[str] = Field(default_factory=list)
    previews: list[PreviewEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    repository_id: str | None = None
    model_version: str | None = None
    preview_version: Literal["adr-applicability-preview-v1"] | None = None

    def validate_for_import(self, repository_id: str) -> None:
        if self.repository_id is not None and self.repository_id != repository_id:
            raise ValueError("Preview belongs to a different repository")
        ids = [entry.requirement_id for entry in self.previews]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Preview must contain nonempty, unique requirement IDs")
        if self.failed or self.errors or self.updated:
            raise ValueError(
                "Import requires a successful extraction preview with no persisted updates"
            )
        if len(ids) != self.planned:
            raise ValueError(
                "Preview is incomplete: result count does not match planned requirements"
            )
        if self.preview_version and (
            not self.repository_id
            or not self.model_version
            or any(e.source is None for e in self.previews)
        ):
            raise ValueError(
                "Versioned preview is missing its repository, model, or source fingerprints"
            )
