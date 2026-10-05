import argparse
import asyncio
import json

from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI

from ai_services.retrievers.hybrid_retrievers import hybrid_retrieve
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.ingestion.rkg.llm_adapters import AzureOpenAILLM
from ai_services.retrievers.retriever_factory import create_retrievers
from shared.utils.env_helper import require_env

GRAPH_RAG_SYSTEM_PROMPT = """
You are an expert software architecture assistant.

Answer the user's question using ONLY the provided GraphRAG context.

The context may contain:
- Relevant entities
- Relevant communities and their summaries
- Community membership
- Graph relationships
- Direct source-code chunks and provenance (file paths, chunk IDs, excerpts)

Rules:
1. Do not invent entities, relationships, files, or architectural details.
2. Treat graph relationships as authoritative.
3. Preserve relationship direction exactly as represented.
4. You may make reasonable architectural inferences, but clearly identify them as inferences.
5. If the retrieved context is insufficient to answer the question, say so.
6. Prefer concrete component names and relationships over vague explanations.
7. Give a concise but useful architectural explanation.
8. When source evidence is provided, use it to ground concrete claims.
9. Do not invent file paths, chunk IDs, or source citations.
"""



async def answer_query(
    query: str,
    context: str,
    llm,
):
    prompt = f"""{GRAPH_RAG_SYSTEM_PROMPT}

User Question:
{query}

GraphRAG Context:
{context}

Answer the user's question based on the GraphRAG context."""

    response = await llm.ainvoke(prompt)

    return response


async def query_graph_rag(
    query: str,
    driver,
    database: str,
    embedder,
    entity_retriever,
    community_retriever,
    chunk_retriever,
    llm,
):
    retrieval = await hybrid_retrieve(
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
    answer = await answer_query(
        query=query,
        context=retrieval.context,
        llm=llm,
    )

    return {
        "answer": answer,
        "sources": retrieval.sources,
    }

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask a question against the DecisionGuard GraphRAG indexes."
    )
    parser.add_argument("query", help="The architecture question to answer.")
    args = parser.parse_args()

    database = require_env("NEO4J_DATABASE") or None
    driver = GraphDatabase.driver(
        require_env("NEO4J_URI"),
        auth=(require_env("NEO4J_USERNAME"), require_env("NEO4J_PASSWORD")),
    )
    try:
        client = AsyncAzureOpenAI(
            azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
            api_key=require_env("AZURE_OPENAI_API_KEY"),
            api_version=require_env("AZURE_OPENAI_API_VERSION"),
        )
        embedder = AzureOpenAIEmbedder(
            client=client,
            deployment=require_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
            dimensions=int(require_env("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
        )
        llm = AzureOpenAILLM(
            client=client,
            deployment=require_env("AZURE_OPENAI_DEPLOYMENT_NAME"),
        )

        driver.verify_connectivity()
        entity_retriever, community_retriever, chunk_retriever = create_retrievers(
            driver=driver,
            database=database,
        )
        result = await query_graph_rag(
            query=args.query,
            driver=driver,
            database=database,
            embedder=embedder,
            entity_retriever=entity_retriever,
            community_retriever=community_retriever,
            chunk_retriever=chunk_retriever,
            llm=llm,
        )

        # print("\nAnswer:\n" + result["answer"])
        print("\nSources:\n" + json.dumps(result["sources"], indent=2))
    finally:
        driver.close()

if __name__ == "__main__":
    asyncio.run(main())
