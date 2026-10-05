from pydantic import BaseModel, Field

class ContextualizedRetrievalQuery(BaseModel):
    query: str = Field(
        min_length=1,
        description="Standalone query suitable for the GraphRAG retrieval pipeline.",
    )

