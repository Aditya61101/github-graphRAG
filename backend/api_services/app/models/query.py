from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    repository_id: str | None = None

class Source(BaseModel):
    file_path: str
    excerpt: str
    score: float | None = None
    score_type: str | None = Field(default=None, description="reranker_relevance for selected evidence; legacy responses may omit it")


class QueryGraphContext(BaseModel):
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)


class APIResponse(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)
    conversation_id: str
    graph_context: QueryGraphContext = Field(default_factory=QueryGraphContext)


QueryResponse = APIResponse
