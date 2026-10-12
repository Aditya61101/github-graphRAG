"""Versioned semantic applicability; graph IDs are resolved by code, never the LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "adr-applicability-v1"
PROMPT_VERSION = "adr-applicability-prompt-v1"
RESOLUTION_VERSION = "adr-applicability-resolution-v1"

Scope = Literal["repository", "community", "entity", "other", "unknown"]
Basis = Literal["explicit", "inferred", "uncertain"]


class ApplicabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ApplicabilityTarget(ApplicabilityModel):
    kind: Literal["entity", "community", "other"]
    name: str
    label: str | None = None
    description: str = ""
    basis: Basis = "uncertain"
    governs_entire_scope: bool = False
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class ApplicabilityCondition(ApplicabilityModel):
    kind: Literal[
        "language",
        "directory",
        "file_category",
        "operation",
        "layer",
        "workflow",
        "other",
    ]
    expression: str
    # Populate selectors only when literal selectors are supported by source text.
    selectors: list[str] = Field(default_factory=list)


class Applicability(ApplicabilityModel):
    scopes: list[Scope] = Field(default_factory=lambda: ["unknown"])
    targets: list[ApplicabilityTarget] = Field(default_factory=list)
    conditions: list[ApplicabilityCondition] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    qualifiers: list[str] = Field(default_factory=list)
    basis: Basis = "uncertain"
    rationale: str = "Applicability has not been established."
    uncertainty: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class ApplicabilityMetadata(ApplicabilityModel):
    schema_version: Literal["adr-applicability-v1"] = SCHEMA_VERSION
    prompt_version: Literal["adr-applicability-prompt-v1"] = PROMPT_VERSION
    applicability: Applicability = Field(default_factory=Applicability)
    entity_ids: list[str] = Field(default_factory=list)
    community_ids: list[str] = Field(default_factory=list)
    unresolved_targets: list[str] = Field(default_factory=list)
    resolution_version: Literal["adr-applicability-resolution-v1"] = RESOLUTION_VERSION
    graph_snapshot: str = ""
    context_truncated: bool = False

    def to_json(self) -> str:
        # Validate even when callers have used model_copy(update=...) or mutated lists.
        return type(self).model_validate(self.model_dump()).model_dump_json()

    @classmethod
    def from_json(cls, value: str | None) -> ApplicabilityMetadata:
        return cls.model_validate_json(value) if value else cls()


def applicability_properties(metadata: ApplicabilityMetadata, model: str) -> dict:
    """Flat, indexable Neo4j properties plus a round-trippable semantic envelope."""
    metadata = ApplicabilityMetadata.from_json(metadata.to_json())
    app = metadata.applicability
    unresolved = (
        not app.scopes
        or "unknown" in app.scopes
        or "other" in app.scopes
        or app.basis != "explicit"
        or bool(app.uncertainty)
        or bool(metadata.unresolved_targets)
        or metadata.context_truncated
        or any(t.basis != "explicit" for t in app.targets)
    )
    return {
        "applicability_json": metadata.to_json(),
        "applicability_schema_version": metadata.schema_version,
        "applicability_prompt_version": metadata.prompt_version,
        "applicability_model": model,
        "applicability_status": "completed",
        "applicability_scopes": list(dict.fromkeys(app.scopes or ["unknown"])),
        "applicability_repository_wide": "repository" in app.scopes,
        "applicability_entity_ids": metadata.entity_ids,
        "applicability_community_ids": metadata.community_ids,
        "applicability_languages": list(
            dict.fromkeys(
                s for c in app.conditions if c.kind == "language" for s in c.selectors
            )
        ),
        "applicability_unresolved": unresolved,
    }
