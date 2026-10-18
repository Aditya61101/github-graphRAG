from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    repository_id: str | None = None

    @field_validator("query")
    @classmethod
    def require_embedding_text(cls, value):
        if not value.strip():
            raise ValueError("Query must contain nonempty text")
        import os
        if os.getenv("AI_PROVIDER", "azure").lower() == "google":
            from shared.ai.embedding_inputs import embedding_windows
            embedding_windows(value)
        return value

class Source(BaseModel):
    file_path: str
    excerpt: str
    score: float | None = None
    score_type: str | None = Field(default=None, description="vector_similarity or reranker_relevance; legacy responses may omit it")


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
