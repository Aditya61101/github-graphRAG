from pydantic import BaseModel, Field

class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)

class Source(BaseModel):
    file_path: str
    excerpt: str
    score: float | None = None

class APIResponse(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)
    conversation_id: str