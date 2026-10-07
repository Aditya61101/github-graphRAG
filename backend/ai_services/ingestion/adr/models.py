from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


@dataclass
class ADRChunk:
    """Represents a structured, content-hashed chunk of an ADR document."""

    chunk_id: str
    adr_id: str
    repository_id: str
    section: str
    chunk_index: int
    text: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None

    @classmethod
    def create(
        cls,
        adr_id: str,
        repository_id: str,
        section: str,
        chunk_index: int,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> ADRChunk:
        content_hash = sha256(text.encode("utf-8")).hexdigest()
        # Deterministic chunk ID scheme
        chunk_id = f"adr:{adr_id}:chunk:{chunk_index}"
        return cls(
            chunk_id=chunk_id,
            adr_id=adr_id,
            repository_id=repository_id,
            section=section,
            chunk_index=chunk_index,
            text=text,
            content_hash=content_hash,
            metadata=metadata or {},
        )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyOutput(StrictModel):
    key: str
    value: str | int | float | bool | list[str | int | float | bool]


class ADRDecisionOutput(StrictModel):
    """Structured architectural decision extracted by LLM from ADR content."""

    title: str = Field(description="Short title or summary of the architectural decision.")
    description: str = Field(description="Detailed rationale and explanation of the decision.")
    decision_type: str | None = Field(
        default=None,
        description="Category such as technology_selection, architectural_pattern, interface_contract, persistence_strategy, security_policy.",
    )
    affects: list[str] = Field(
        default_factory=list,
        description="Names of components or services directly affected by this decision.",
    )
    constrains: list[str] = Field(
        default_factory=list,
        description="Names of components or services restricted or constrained by this decision.",
    )


class ADRConstraintOutput(StrictModel):
    """Structured architectural constraint extracted by LLM from ADR content."""

    description: str = Field(description="Specific architectural constraint, rule, or boundary.")
    constraint_type: str | None = Field(
        default=None,
        description="Category such as communication_rule, data_access_rule, security_constraint.",
    )
    target_entities: list[str] = Field(
        default_factory=list,
        description="Names of components or services subjected to this constraint.",
    )


class ADREntityOutput(StrictModel):
    """Architectural component or service identified in the ADR."""

    name: str = Field(description="Name of the architectural entity, service, database, or component.")
    label: str = Field(
        default="Component",
        description="Entity type/label: Service, Database, Queue, Interface, Component, Library.",
    )
    properties: list[PropertyOutput] = Field(default_factory=list)


class ADRRelationshipOutput(StrictModel):
    """Architectural relationship between two components identified in the ADR."""

    source_name: str = Field(description="Source entity name.")
    source_label: str = Field(default="Component", description="Source entity label.")
    relationship_type: str = Field(
        description="Relationship type: USES, DEPENDS_ON, CONNECTS_TO, CALLS, READS_FROM, WRITES_TO."
    )
    target_name: str = Field(description="Target entity name.")
    target_label: str = Field(default="Component", description="Target entity label.")
    rationale: str | None = Field(default=None, description="Architectural reasoning.")


class ADRExtractionResult(StrictModel):
    """Combined structured extraction result for an ADR document or chunk."""

    decisions: list[ADRDecisionOutput] = Field(default_factory=list)
    constraints: list[ADRConstraintOutput] = Field(default_factory=list)
    entities: list[ADREntityOutput] = Field(default_factory=list)
    relationships: list[ADRRelationshipOutput] = Field(default_factory=list)


@dataclass
class ArchitecturalDecisionNode:
    """Graph representation of an ArchitecturalDecision node in Neo4j."""

    id: str
    adr_id: str
    repository_id: str
    title: str
    description: str
    decision_type: str | None
    affects_entity_ids: list[str] = field(default_factory=list)
    constrains_entity_ids: list[str] = field(default_factory=list)
    evidence_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class ArchitecturalConstraintNode:
    """Graph representation of an ArchitecturalConstraint node in Neo4j."""

    id: str
    adr_id: str
    repository_id: str
    description: str
    constraint_type: str | None
    constrains_entity_ids: list[str] = field(default_factory=list)
    evidence_chunk_ids: list[str] = field(default_factory=list)

