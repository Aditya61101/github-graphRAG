from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ai_services.retrievers.hybrid_retrievers import (
    HybridRetrievalResult,
    hybrid_retrieve,
)
from ai_services.retrievers.reranker import Reranker
from ai_services.retrievers.settings import RetrievalSettings


def create_query_graph_rag_tool(
    driver,
    database: str | None,
    embedder,
    entity_retriever,
    community_retriever,
    chunk_retriever,
    reranker: Reranker,
    retrieval_settings: RetrievalSettings | None = None,
):
    @tool
    async def query_graph_rag(
        query: str,
        runtime: ToolRuntime,
        repository_id: str | None = None,
    ) -> Command:
        """Search the repository knowledge graph for evidence relevant to the query.

        The tool returns only retrieval context to the model. Structured source
        metadata is stored separately in the current agent run so it can be
        returned by the API without inflating the model conversation history.

        Args:
            query: Question or architectural concept to search for.
            runtime: Runtime context providing graph state.
            repository_id: Optional globally unique repository identifier to scope retrieval.
        """
        # Resolve repository_id from tool argument or from graph run state
        state_repo = None
        if hasattr(runtime, "state") and isinstance(runtime.state, dict):
            state_repo = runtime.state.get("repository_id")
        # The API-authorized scope cannot be overridden by a model tool argument.
        if state_repo and repository_id and repository_id != state_repo:
            raise ValueError("Tool repository does not match the authorized repository")
        if not state_repo:
            raise ValueError('Missing backend-authorized repository scope')
        target_repo_id = state_repo

        result: HybridRetrievalResult = await hybrid_retrieve(
            query=query,
            driver=driver,
            database=database,
            embedder=embedder,
            entity_retriever=entity_retriever,
            community_retriever=community_retriever,
            chunk_retriever=chunk_retriever,
            repository_id=target_repo_id,
            reranker=reranker,
            settings=retrieval_settings,
        )

        return Command(
            update={
                "retrieval_sources": result.sources,
                "retrieval_graph_context": result.graph_context.to_dict(),
                "messages": [
                    ToolMessage(
                        content=result.context,
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    return query_graph_rag
