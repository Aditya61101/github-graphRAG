from langchain.tools import tool

from ai_services.retrievers.hybrid_retrievers import (
    HybridRetrievalResult,
    hybrid_retrieve,
)


def create_query_graph_rag_tool(
    driver,
    database: str,
    embedder,
    entity_retriever,
    community_retriever,
    chunk_retriever,
):
    @tool
    async def query_graph_rag(query: str) -> HybridRetrievalResult:
        """
        Search the repository knowledge graph for entities, relationships,
        communities, and supporting evidence relevant to the query.
        """
        return await hybrid_retrieve(
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

    return query_graph_rag