from pydantic import BaseModel, Field

class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    
class QueryPreparationResult(BaseModel):
    conversation_id: str
    original_query: str
    contextualized_query: str
    
class SourceChunk(BaseModel):
    chunk_id: str
    file_path: str | None = None
    excerpt: str
    score: float | None = None


class SourceFile(BaseModel):
    file_path: str
    chunks: list[SourceChunk] = Field(default_factory=list)


class APIResponse(BaseModel):
    answer: str
    sources: list[SourceFile] = Field(default_factory=list)
    conversation_id: str