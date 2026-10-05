"""Domain models for the Community Layer."""

from __future__ import annotations

import hashlib
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model forbidding unexpected extra fields."""

    model_config = ConfigDict(extra="forbid")


class CommunityMember(BaseModel):
    """An architectural entity belonging to a community."""

    entity_id: str
    entity_name: str
    labels: list[str] = Field(default_factory=list)

    @property
    def primary_label(self) -> str:
        """Return the most specific semantic label, filtering out generic internal labels."""
        for lbl in self.labels:
            if lbl not in {"__Entity__", "__KGBuilder__", "Entity"}:
                return lbl
        return self.labels[0] if self.labels else "Entity"


class DirectedRelationship(BaseModel):
    """A directed architectural relationship between two entities within the same community."""

    source_id: str
    source_name: str
    relationship_type: str
    target_id: str
    target_name: str


class CommunityContext(BaseModel):
    """The architectural context of a community for summarization."""

    community_id: str | int
    members: list[CommunityMember] = Field(default_factory=list)
    relationships: list[DirectedRelationship] = Field(default_factory=list)

    def context_hash(self) -> str:
        """Deterministic hash of canonical entity IDs, names, labels, and directed edges."""
        sorted_members = sorted(
            f"{m.entity_id}:{m.entity_name}:{','.join(sorted(m.labels))}"
            for m in self.members
        )
        sorted_rels = sorted(
            f"{r.source_id}:{r.source_name}-[{r.relationship_type}]->{r.target_id}:{r.target_name}"
            for r in self.relationships
        )
        raw = f"MEMBERS:{'|'.join(sorted_members)}#RELS:{'|'.join(sorted_rels)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def format_text(self) -> str:
        """Format human-readable context for LLM summarization, strictly preserving edge direction."""
        lines = [
            f"Community ID: {self.community_id}",
            "",
            "Entities:",
        ]
        for m in sorted(self.members, key=lambda x: x.entity_name):
            lines.append(f"- {m.entity_name} [{m.primary_label}] (ID: {m.entity_id})")

        lines.append("")
        lines.append("Directed Relationships:")
        if self.relationships:
            for r in sorted(
                self.relationships,
                key=lambda x: (x.source_name, x.relationship_type, x.target_name),
            ):
                lines.append(f"- {r.source_name} -[{r.relationship_type}]-> {r.target_name}")
        else:
            lines.append("- (no internal relationships)")

        return "\n".join(lines)


class CommunitySummaryOutput(StrictModel):
    """Structured output expected from the summarization LLM."""

    architectural_role: str = Field(
        description="The primary architectural role or responsibility of this community."
    )
    summary: str = Field(
        description="Concise architectural summary explaining what this group does and how components interact."
    )
    key_entities: list[str] = Field(
        default_factory=list,
        description="Names of key architectural components in this community.",
    )

    @field_validator("architectural_role", mode="after")
    @classmethod
    def validate_role(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("architectural_role must not be empty.")
        if len(cleaned) > 200:
            raise ValueError(f"architectural_role exceeds max length of 200 chars ({len(cleaned)} chars).")
        return cleaned

    @field_validator("summary", mode="after")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be empty.")
        if len(cleaned) > 2000:
            raise ValueError(f"summary exceeds max length of 2000 chars ({len(cleaned)} chars).")
        return cleaned

    @field_validator("key_entities", mode="after")
    @classmethod
    def validate_key_entities(cls, entities: list[str]) -> list[str]:
        cleaned = [e.strip() for e in entities if e and e.strip()]
        if len(cleaned) > 20:
            cleaned = cleaned[:20]
        return cleaned


class CommunitySummary(BaseModel):
    """Validated architectural summary for a community with content hashes for change tracking."""

    community_id: str | int
    summary: str
    architectural_role: str
    key_entities: list[str] = Field(default_factory=list)
    summary_hash: str
    context_hash: str

    @classmethod
    def create(
        cls,
        community_id: str | int,
        output: CommunitySummaryOutput,
        context_hash: str,
    ) -> CommunitySummary:
        raw = f"{output.architectural_role.strip()}:{output.summary.strip()}:{sorted(e.strip() for e in output.key_entities)}"
        s_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return cls(
            community_id=community_id,
            summary=output.summary.strip(),
            architectural_role=output.architectural_role.strip(),
            key_entities=[e.strip() for e in output.key_entities if e.strip()],
            summary_hash=s_hash,
            context_hash=context_hash,
        )


class CommunityEmbedding(BaseModel):
    """Vector embedding of a community's architectural summary."""

    community_id: str | int
    embedding: list[float]
    dimensions: int
    summary_hash: str


class Community(BaseModel):
    """Domain model representing a Community node in Neo4j."""

    community_id: str | int
    entity_count: int
    summary: str | None = None
    architectural_role: str | None = None
    key_entities: list[str] = Field(default_factory=list)
    summary_hash: str | None = None
    context_hash: str | None = None
    embedding: list[float] | None = None


class CommunityProcessingState(BaseModel):
    """Persisted processing state for a community used for fine-grained change detection."""

    community_id: str | int
    has_summary: bool = False
    summary: str | None = None
    architectural_role: str | None = None
    key_entities: list[str] = Field(default_factory=list)
    summary_hash: str | None = None
    context_hash: str | None = None
    has_embedding: bool = False
    embedded_summary_hash: str | None = None


class CommunityDetectionResult(BaseModel):
    """Result of a community detection algorithm run."""

    algorithm: str
    community_count: int
    node_count: int
    assignments: dict[str | int, list[str]] = Field(
        default_factory=dict,
        description="Mapping from final community ID to list of member entity IDs.",
    )
