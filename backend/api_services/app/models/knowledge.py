from typing import Literal
from pydantic import BaseModel, Field


class KnowledgeSource(BaseModel):
    id: str
    title: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    source_version: str | None = None
    content_hash: str | None = None


class KnowledgeItem(BaseModel):
    id: str
    kind: Literal['decision', 'constraint']
    title: str | None = None
    description: str
    type: str | None = None
    sources: list[KnowledgeSource] = Field(default_factory=list)


class KnowledgePage(BaseModel):
    items: list[KnowledgeItem]
    total: int
    offset: int
    limit: int


class KnowledgeEntity(BaseModel):
    id: str
    name: str | None = None
    label: str | None = None
    relationship: Literal['AFFECTS', 'CONSTRAINS']
    source: Literal['ADR'] = 'ADR'


class KnowledgeEvidence(BaseModel):
    id: str
    adr_id: str
    section: str | None = None
    chunk_index: int | None = None
    excerpt: str
    truncated: bool
    content_hash: str | None = None


class KnowledgeDetail(KnowledgeItem):
    entities: list[KnowledgeEntity]
    evidence: list[KnowledgeEvidence]
    entities_truncated: bool = False
    evidence_truncated: bool = False
    relationship_basis: Literal['documented_ADR_knowledge'] = 'documented_ADR_knowledge'
