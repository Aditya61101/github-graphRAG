from typing import Protocol, Sequence

from .models import (
    CandidateKnowledge,
    EvidenceChunk,
    RelationshipCandidate,
    ValidatedRelationship,
)

class LocalExtractor(Protocol):
    async def extract(
        self,
        chunks: Sequence[EvidenceChunk],
        *,
        examples: str = "",
    ) -> list[CandidateKnowledge]: ...


class RelationshipValidator(Protocol):
    async def validate(
        self,
        candidate: RelationshipCandidate,
        *,
        evidence: Sequence[EvidenceChunk],
        neighborhood: str,
    ) -> ValidatedRelationship | None: ...


class CandidateKnowledgeStore(Protocol):
    async def put(self, candidate: CandidateKnowledge) -> None: ...
    async def get_for_chunk(self, chunk_id: str) -> CandidateKnowledge | None: ...
    async def all(self) -> list[CandidateKnowledge]: ...
