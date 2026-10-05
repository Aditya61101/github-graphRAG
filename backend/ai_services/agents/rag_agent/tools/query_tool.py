from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ai_services.retrievers.hybrid_retrievers import (
    HybridRetrievalResult,
    hybrid_retrieve,
)


def create_query_graph_rag_tool(
    driver,
    database: str | None,
    embedder,
    entity_retriever,
    community_retriever,
    chunk_retriever,
):
    @tool
    async def query_graph_rag(
        query: str,
        runtime: ToolRuntime,
    ) -> Command:
        """Search the repository knowledge graph for evidence relevant to the query.

        The tool returns only retrieval context to the model. Structured source
        metadata is stored separately in the current agent run so it can be
        returned by the API without inflating the model conversation history.
        """
        result: HybridRetrievalResult = await hybrid_retrieve(
            query=query,
            driver=driver,
            database=database,
            embedder=embedder,
            entity_retriever=entity_retriever,
            community_retriever=community_retriever,
            chunk_retriever=chunk_retriever,
            entity_top_k=5,
            community_top_k=3,
            chunk_top_k=5,
        )

        return Command(
            update={
                "retrieval_sources": result.sources,
                "messages": [
                    ToolMessage(
                        content=result.context,
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    return query_graph_rag
