from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from ai_services.ingestion.pr.models import ContentStatus

from .models import SHA, BaselineSnapshot, Contract


class SourceBlob(Contract):
    repository_id: str
    commit_sha: SHA
    path: str
    status: ContentStatus
    text: str | None = None


class RevisionSourceReader(Protocol):
    def read_blob(
        self, repository_id: str, commit_sha: str, path: str
    ) -> SourceBlob: ...


class BaselineReadiness(Protocol):
    """Must consult authoritative readiness under the same graph mutation lock.

    Phase 4.3 supplies durable generation/dirty/run tracking. SQLite SHA alone
    cannot implement this interface safely after partial graph publication.
    """

    async def snapshot(self, repository_id: str) -> BaselineSnapshot: ...


@dataclass(frozen=True)
class Page:
    rows: tuple[dict[str, Any], ...]
    truncated: bool = False


class ScopedGraphSnapshot(Protocol):
    def file_chunks(self, paths: tuple[str, ...], limit: int) -> Page: ...
    def relationships(self, entity_ids: tuple[str, ...], limit: int) -> Page: ...
    def adr_catalog(self, limit: int) -> Page: ...
    def communities(self, entity_ids: tuple[str, ...], limit: int) -> Page: ...
    def applicability_evidence(
        self, adr_ids: tuple[str, ...], chunk_ids: tuple[str, ...], limit: int
    ) -> Page: ...
    def requirements(
        self,
        entity_ids: tuple[str, ...],
        limit: int,
        *,
        fallback: bool = False,
        community_ids: tuple[str, ...] = (),
        languages: tuple[str, ...] = (),
    ) -> Page: ...


class ArchitectureGraphReader(Protocol):
    def read_snapshot(
        self, repository_id: str
    ) -> AbstractContextManager[ScopedGraphSnapshot]: ...
