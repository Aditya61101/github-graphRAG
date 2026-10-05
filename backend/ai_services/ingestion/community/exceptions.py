"""Custom exception hierarchy for the Community Layer."""

from __future__ import annotations


class CommunityError(Exception):
    """Base exception for all community layer errors."""


class CommunityConfigurationError(CommunityError):
    """Raised when configuration values are invalid or unsafe."""


class CommunityProjectionError(CommunityError):
    """Raised when graph projection fails (e.g. empty graph, invalid relation types, GDS failure)."""


class CommunityDetectionError(CommunityError):
    """Raised when community detection algorithm execution fails."""


class CommunityPersistenceError(CommunityError):
    """Raised when persisting community data, nodes, or membership fails."""


class CommunitySummaryError(CommunityError):
    """Raised when community summarization fails for a specific community."""

    def __init__(
        self,
        message: str,
        community_id: str | int | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.community_id = community_id
        prefix = f"[Community {community_id}] " if community_id is not None else ""
        super().__init__(f"{prefix}{message}")
        if cause is not None:
            self.__cause__ = cause


class CommunityEmbeddingError(CommunityError):
    """Raised when community embedding fails for affected communities."""

    def __init__(
        self,
        message: str,
        community_ids: list[str | int] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.community_ids = community_ids or []
        prefix = f"[Communities {self.community_ids}] " if self.community_ids else ""
        super().__init__(f"{prefix}{message}")
        if cause is not None:
            self.__cause__ = cause


class CommunityVectorIndexError(CommunityError):
    """Raised when vector index verification, creation, or dimension check fails."""
