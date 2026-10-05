from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ChunkStrategy(str, Enum):
    SYMBOL = "symbol"
    MARKDOWN_SECTION = "markdown_section"
    OPENAPI_OPERATION = "openapi_operation"
    PROTO_MESSAGE = "proto_message"
    CONFIG_SECTION = "config_section"
    WHOLE_FILE = "whole_file"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class EvidenceChunk:
    """Raw repository evidence produced by the strategy-aware splitter."""

    chunk_id: str
    repository: str
    commit: str
    file_path: str
    chunk_index: int
    text: str
    content_hash: str
    strategy: ChunkStrategy
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None

    @classmethod
    def create(
        cls,
        repository,
        commit,
        file_path,
        chunk_index,
        text,
        strategy,
        metadata=None,
    ):
        content_hash = sha256(text.encode()).hexdigest()
        chunk_id = sha256(
            f"{repository}:{commit}:{file_path}:{chunk_index}:{content_hash}".encode()
        ).hexdigest()
        return cls(
            chunk_id,
            repository,
            commit,
            file_path,
            chunk_index,
            text,
            content_hash,
            strategy,
            metadata or {},
        )


from typing import TypeAlias

Neo4jScalar: TypeAlias = str | int | float | bool
Neo4jPropertyValue: TypeAlias = Neo4jScalar | list[Neo4jScalar]

class ExtractedEntity(BaseModel):
    label: str
    name: str
    properties: dict[str, Neo4jPropertyValue] = Field(default_factory=dict)
    source_chunk_ids: list[str] = Field(default_factory=list)

    @field_validator("label", "name", mode="before")
    @classmethod
    def require_nonempty_identity(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Entity label and name must be non-empty strings.")
        return value.strip()


class ExtractedRelationship(BaseModel):
    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: dict[str, Neo4jPropertyValue] = Field(default_factory=dict)
    source_chunk_ids: list[str] = Field(default_factory=list)


class CandidateKnowledge(BaseModel):
    """LLM-derived architectural knowledge that is still non-authoritative."""

    chunk_id: str
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)


class CanonicalEntity(BaseModel):
    canonical_id: str
    label: str
    name: str
    properties: dict[str, Neo4jPropertyValue] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None


class RelationshipCandidate(BaseModel):
    source_id: str
    target_id: str
    relationship_type: str
    properties: dict[str, Neo4jPropertyValue] = Field(default_factory=dict)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    reason: str


class ValidatedRelationship(BaseModel):
    source_id: str
    target_id: str
    relationship_type: str
    properties: dict[str, Neo4jPropertyValue] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    rationale: str
