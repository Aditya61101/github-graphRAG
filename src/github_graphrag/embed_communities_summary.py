from neo4j_graphrag.indexes import create_vector_index, upsert_vectors
from neo4j_graphrag.types import EntityType

COMMUNITY_INDEX_NAME = "community_vector_index"
EMBEDDING_PROPERTY = "embedding"
EMBEDDING_DIMENSIONS = 768

def load_community_summaries(driver, database: str):
    result = driver.execute_query(
        """
        MATCH (c:Community)
        WHERE c.summary IS NOT NULL
        RETURN
            elementId(c) AS community_id,
            c.communityId AS community_number,
            c.summary AS summary
        ORDER BY community_number
        """,
        database_=database,
    )

    return result.records

def embed_communities(
    driver,
    database: str,
    embedder,
):
    create_vector_index(
        driver,
        COMMUNITY_INDEX_NAME,
        label="Community",
        embedding_property=EMBEDDING_PROPERTY,
        dimensions=EMBEDDING_DIMENSIONS,
        similarity_fn="cosine",
        neo4j_database=database,
    )

    communities = load_community_summaries(
        driver,
        database,
    )

    print(f"Found {len(communities)} communities.")

    for community in communities:

        embedding_text = community["summary"]

        print("\n--------------------------------")
        print(embedding_text)

        vector = embedder.embed_query(
            embedding_text
        )

        if len(vector) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Expected {EMBEDDING_DIMENSIONS} dimensions, "
                f"got {len(vector)}."
            )

        upsert_vectors(
            driver,
            ids=[community["community_id"]],
            embedding_property=EMBEDDING_PROPERTY,
            embeddings=[vector],
            entity_type=EntityType.NODE,
            neo4j_database=database,
        )


if __name__ == "__main__":
    import os
    from github_graphrag.graphdb_driver import CreateDriver
    from neo4j_graphrag.embeddings import GeminiEmbedder

    database = os.getenv("NEO4J_DATABASE") or None

    uri = os.getenv("NEO4J_URI")
    username = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")

    embedder = GeminiEmbedder(
        model="gemini-embedding-001",
        embedding_dim=EMBEDDING_DIMENSIONS,
        api_key=api_key,
    )

    driver = CreateDriver(uri, username, password).get_driver()

    try:
        driver.verify_connectivity()
        embed_communities(driver, database, embedder)
    finally:
        driver.close()