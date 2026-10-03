"""Interfaces and protocols for the Community Layer."""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

# Reuse the existing provider-agnostic Embedder abstraction
from github_graphrag.embeddings.base import Embedder

from .models import (
    Community,
    CommunityContext,
    CommunityDetectionResult,
    CommunityEmbedding,
    CommunityProcessingState,
    CommunitySummary,
)


@runtime_checkable
class SummarizationLLM(Protocol):
    """Protocol for LLM client used in community summarization."""

    async def ainvoke(self, prompt: str) -> str | Any:
        """Asynchronously invoke the LLM with a prompt and return text response or response object."""
        ...


@runtime_checkable
class CommunityIdentityStrategy(Protocol):
    """Strategy to derive persistent community identities from detection results."""

    def generate_id(
        self,
        raw_community_id: int | str,
        member_ids: Sequence[str],
    ) -> str | int:
        """Derive a final community ID for the snapshot."""
        ...


@runtime_checkable
class CommunityDetector(Protocol):
    """Detects communities within a projected graph."""

    async def detect(
        self,
        driver: Any,
        graph_name: str,
        database: str | None = None,
    ) -> CommunityDetectionResult:
        """Execute community detection on the named projection and return domain assignments."""
        ...


@runtime_checkable
class CommunityStore(Protocol):
    """Persistent storage abstraction for community nodes, memberships, contexts, and summaries."""

    async def persist_memberships(
        self,
        assignments: dict[str | int, list[str]],
        is_full_sync: bool = True,
    ) -> int:
        """Persist community assignments to the graph idempotently.

        Args:
            assignments: Mapping from final community ID to member entity IDs.
            is_full_sync: If True, performs global synchronization (deletes empty communities).
                          If False, only updates memberships for the specified communities.
        Returns:
            Number of entity memberships successfully persisted.
        """
        ...

    async def cleanup_temporary_detection_properties(self) -> None:
        """Remove temporary GDS partition properties (e.g. _gdsCommunityId) from entity nodes."""
        ...

    async def load_community_contexts(
        self,
        community_ids: Sequence[str | int] | None = None,
    ) -> list[CommunityContext]:
        """Load internal architectural contexts for the requested communities."""
        ...

    async def get_processing_states(
        self,
        community_ids: Sequence[str | int],
    ) -> dict[str | int, CommunityProcessingState]:
        """Return persisted processing state (summary & embedding status) for community nodes."""
        ...

    async def persist_summaries(
        self,
        summaries: Sequence[CommunitySummary],
    ) -> None:
        """Persist architectural summaries to Community nodes."""
        ...

    async def persist_embeddings(
        self,
        embeddings: Sequence[CommunityEmbedding],
        embedding_version_hash: str | None = None,
    ) -> None:
        """Persist vector embeddings and embedded version hash to Community nodes."""
        ...

    async def get_communities(
        self,
        community_ids: Sequence[str | int] | None = None,
    ) -> list[Community]:
        """Retrieve Community domain models."""
        ...


@runtime_checkable
class CommunitySummarizer(Protocol):
    """Synthesizes architectural summaries from community contexts."""

    async def summarize(self, context: CommunityContext) -> CommunitySummary:
        """Generate an architectural summary for a single community context."""
        ...

    async def summarize_batch(
        self,
        contexts: Sequence[CommunityContext],
        existing_states: dict[str | int, CommunityProcessingState] | None = None,
        force_refresh: bool = False,
    ) -> tuple[list[CommunitySummary], list[CommunitySummary]]:
        """Process multiple community contexts.

        Returns:
            (newly_generated_summaries, reused_existing_summaries)
        """
        ...


@runtime_checkable
class VectorIndexManager(Protocol):
    """Manages the Neo4j vector index for community embeddings."""

    async def ensure_index(self, expected_dimensions: int) -> None:
        """Validate or create the community vector index with the expected dimensions."""
        ...

    async def await_online(
        self,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Wait until the vector index reaches ONLINE state."""
        ...
