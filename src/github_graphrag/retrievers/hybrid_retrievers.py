from github_graphrag.graph_expansion import expand_entities
from github_graphrag.retrievers.context_formatter import format_retrieval_context

def load_communities(
    driver,
    database: str,
    community_ids: list[int],
):
    if not community_ids:
        return []

    result = driver.execute_query(
        """
        MATCH (c:Community)
        WHERE c.communityId IN $community_ids

        OPTIONAL MATCH (e:__Entity__)-[:MEMBER_OF]->(c)

        RETURN
            c.communityId AS community_id,
            c.summary AS summary,
            collect(DISTINCT e.name) AS entities

        ORDER BY community_id
        """,
        community_ids=community_ids,
        database_=database,
    )

    return result.records


def hybrid_retrieve(
    query: str,
    driver,
    database: str,
    entity_retriever,
    community_retriever,
    entity_top_k: int = 5,
    community_top_k: int = 3,
):
    # ---------------------------------------
    # 1. Entity vector retrieval
    # ---------------------------------------

    entity_results = entity_retriever.search(
        query_text=query,
        top_k=entity_top_k,
    )

    entity_names = [
        item.content
        for item in entity_results.items
    ]

    # ---------------------------------------
    # 2. Graph expansion
    # ---------------------------------------

    graph_records = expand_entities(
        driver,
        entity_names,
        database,
    )
    
    # ---------------------------------------
    # 3. Community vector retrieval
    # ---------------------------------------
    community_results = community_retriever.search(
        query_text=query,
        top_k=community_top_k,
    )
    community_ids = [
        item.metadata["community_id"]
        for item in community_results.items
    ]

    # ---------------------------------------
    # 4. Load complete community information
    # ---------------------------------------
    communities = load_communities(
        driver,
        database,
        community_ids,
    )

    # ---------------------------------------
    # 5. Format everything
    # ---------------------------------------
    context = format_retrieval_context(
        entity_results=entity_results,
        community_results=community_results,
        communities=communities,
        graph_records=graph_records,
    )

    return context

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    from github_graphrag.graphdb_driver import CreateDriver
    from neo4j_graphrag.embeddings import GeminiEmbedder
    from github_graphrag.retrievers.retriever_factory import create_retrievers

    uri = os.getenv("NEO4J_URI")
    username = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE") or None
    api_key = os.getenv("GEMINI_API_KEY")

    driver = CreateDriver(uri, username, password).get_driver()

    embedder = GeminiEmbedder(
        model="gemini-embedding-001",
        embedding_dim=768,
        api_key=api_key,
    )

    entity_retriever, community_retriever = create_retrievers(
        driver=driver,
        embedder=embedder,
    )

    query = "Which components are responsible for user authentication?"

    context = hybrid_retrieve(
        query=query,
        driver=driver,
        database=database,
        entity_retriever=entity_retriever,
        community_retriever=community_retriever,
        entity_top_k=5,
        community_top_k=3,
    )

    print("\nContext:\n", context)