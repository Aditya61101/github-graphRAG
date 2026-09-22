from github_graphrag.retrievers.hybrid_retrievers import hybrid_retrieve


GRAPH_RAG_SYSTEM_PROMPT = """
You are an expert software architecture assistant.

Answer the user's question using ONLY the provided GraphRAG context.

The context may contain:
- Relevant entities
- Relevant communities and their summaries
- Community membership
- Graph relationships

Rules:
1. Do not invent entities, relationships, files, or architectural details.
2. Treat graph relationships as authoritative.
3. Preserve relationship direction exactly as represented.
4. You may make reasonable architectural inferences, but clearly identify them as inferences.
5. If the retrieved context is insufficient to answer the question, say so.
6. Prefer concrete component names and relationships over vague explanations.
7. Give a concise but useful architectural explanation.
"""

def answer_query(
    query: str,
    context: str,
    llm,
):
    prompt = f"""
User Question:
{query}

GraphRAG Context:
{context}

Answer the user's question based on the GraphRAG context.
"""

    response = llm.invoke(
        [
            {
                "role": "system",
                "content": GRAPH_RAG_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]
    )

    return response.content

def query_graph_rag(
    query: str,
    driver,
    database: str,
    entity_retriever,
    community_retriever,
    llm,
):
    # 1. Retrieve relevant graph context
    context = hybrid_retrieve(
        query=query,
        driver=driver,
        database=database,
        entity_retriever=entity_retriever,
        community_retriever=community_retriever,
        entity_top_k=5,
        community_top_k=3,
    )

    # 2. Generate answer
    answer = answer_query(
        query=query,
        context=context,
        llm=llm,
    )

    return answer