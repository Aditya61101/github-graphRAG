"""Configuration for the Community Layer."""

from __future__ import annotations

import re
from pydantic import BaseModel, Field, field_validator

from .exceptions import CommunityConfigurationError

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CommunityConfig(BaseModel):
    """Configuration settings for community detection, summarization, and vector indexing."""

    graph_name: str = Field(
        default="entityGraph",
        description="Name of the projected graph in Neo4j Graph Data Science (GDS).",
    )
    algorithm: str = Field(
        default="leiden",
        description="Community detection algorithm to use ('leiden' or 'louvain').",
    )
    random_seed: int = Field(
        default=42,
        description="Deterministic random seed for algorithms supporting it.",
    )
    gds_memory: str = Field(
        default="2GB",
        description="Memory allocation for GDS projection.",
    )
    gds_ttl: str = Field(
        default="PT30M",
        description="Time-to-live for the GDS projected graph (ISO-8601 duration).",
    )
    entity_label: str = Field(
        default="Entity",
        description="Neo4j node label identifying architectural entities.",
    )
    temporary_gds_property: str = Field(
        default="_gdsCommunityId",
        description="Temporary node property used by GDS before final ID resolution.",
    )
    community_label: str = Field(
        default="Community",
        description="Neo4j node label representing community nodes.",
    )
    membership_relationship: str = Field(
        default="MEMBER_OF",
        description="Relationship type from entity to community node.",
    )
    vector_index_name: str = Field(
        default="community_vector_index",
        description="Name of the Neo4j vector index for community embeddings.",
    )
    embedding_property: str = Field(
        default="embedding",
        description="Node property key storing the community embedding vector.",
    )
    embedding_batch_size: int = Field(
        default=32,
        description="Maximum number of community summaries to embed in one batch.",
    )
    summary_concurrency: int = Field(
        default=4,
        description="Maximum concurrent LLM calls for community summarization.",
    )
    similarity_function: str = Field(
        default="cosine",
        description="Vector similarity function ('cosine' or 'euclidean').",
    )
    include_key_entities_in_embedding: bool = Field(
        default=True,
        description="Whether to include key entity names in the generated embedding text.",
    )
    vector_index_await_timeout: float = Field(
        default=30.0,
        description="Max seconds to await vector index readiness (0 to disable waiting).",
    )
    vector_index_poll_interval: float = Field(
        default=0.5,
        description="Polling interval in seconds while awaiting vector index readiness.",
    )
    database: str | None = Field(
        default=None,
        description="Target Neo4j database name (None uses the driver default).",
    )
    repository_id: str | None = Field(
        default=None,
        description="Target repository identifier to scope community detection and persistence.",
    )

    @property
    def effective_graph_name(self) -> str:
        if self.repository_id and self.graph_name == "entityGraph":
            # Sanitize repository ID to form a valid GDS identifier
            clean_id = re.sub(r"[^A-Za-z0-9_]", "_", self.repository_id)
            return f"entityGraph_{clean_id}"
        return self.graph_name

    @field_validator(
        "entity_label",
        "temporary_gds_property",
        "community_label",
        "membership_relationship",
        "embedding_property",
        mode="after",
    )
    @classmethod
    def validate_cypher_identifiers(cls, value: str) -> str:
        if not _IDENTIFIER_PATTERN.match(value):
            raise CommunityConfigurationError(
                f"Invalid Cypher identifier: '{value}'. "
                "Must begin with a letter or underscore and contain only alphanumeric or underscore characters."
            )
        return value

    @field_validator("similarity_function", mode="after")
    @classmethod
    def validate_similarity_function(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"cosine", "euclidean"}:
            raise CommunityConfigurationError(
                f"Invalid similarity function: '{value}'. Must be 'cosine' or 'euclidean'."
            )
        return normalized

    @field_validator("algorithm", mode="after")
    @classmethod
    def validate_algorithm(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"leiden", "louvain"}:
            raise CommunityConfigurationError(
                f"Unsupported community algorithm: '{value}'. Supported: 'leiden', 'louvain'."
            )
        return normalized
