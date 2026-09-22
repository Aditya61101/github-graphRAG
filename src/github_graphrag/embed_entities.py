from __future__ import annotations

import os

from dotenv import load_dotenv
load_dotenv()
from neo4j_graphrag.embeddings import GeminiEmbedder
from neo4j_graphrag.indexes import create_vector_index, upsert_vectors
from neo4j_graphrag.types import EntityType

INDEX_NAME = "entity_vector_index"
EMBEDDING_PROPERTY = "embedding"
EMBEDDING_DIMENSIONS = 768


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"{name} is required.")

    return value


def load_entities(driver, database: str | None) -> list[dict]:
    records, _, _ = driver.execute_query(
        """
        MATCH (e:__Entity__)
        RETURN
            elementId(e) AS id,
            e.name AS name,
            labels(e) AS labels
        ORDER BY e.name
        """,
        database_=database,
    )

    return [dict(record) for record in records]


def load_relationships(
    driver,
    entity_id: str,
    database: str | None,
) -> list[dict]:

    records, _, _ = driver.execute_query(
        """
        MATCH (e:__Entity__)-[r]->(target:__Entity__)
        WHERE elementId(e) = $entity_id

        RETURN
            type(r) AS relationship,
            target.name AS target
        ORDER BY relationship, target
        """,
        entity_id=entity_id,
        database_=database,
    )

    return [dict(record) for record in records]


def build_embedding_text(
    entity: dict,
    relationships: list[dict],
) -> str:

    entity_labels = [
        label
        for label in entity["labels"]
        if label not in {"__Entity__", "__KGBuilder__"}
    ]

    lines = [
        f"Entity: {entity['name']}",
        f"Type: {', '.join(entity_labels)}",
        "",
        "Relationships:",
    ]

    if relationships:
        for relationship in relationships:
            lines.append(
                f"- {relationship['relationship']} "
                f"-> {relationship['target']}"
            )
    else:
        lines.append("- None")

    return "\n".join(lines)


def create_entity_embeddings(
    driver,
    database: str,
    embedder
) -> None:
    # try:
        # driver.verify_connectivity()

    # Create Neo4j vector index.
    create_vector_index(
        driver,
        INDEX_NAME,
        label="__Entity__",
        embedding_property=EMBEDDING_PROPERTY,
        dimensions=EMBEDDING_DIMENSIONS,
        similarity_fn="cosine",
        neo4j_database=database,
    )

    entities = load_entities(
        driver,
        database,
    )

    print(f"Found {len(entities)} entities.")

    for entity in entities:
        relationships = load_relationships(
            driver,
            entity["id"],
            database,
        )

        embedding_text = build_embedding_text(
            entity,
            relationships,
        )

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
            ids=[entity["id"]],
            embedding_property=EMBEDDING_PROPERTY,
            embeddings=[vector],
            entity_type=EntityType.NODE,
            neo4j_database=database,
        )

        print(
            f"Embedded: {entity['name']} "
            f"({len(vector)} dimensions)"
        )

    print(
        f"\nSuccessfully embedded "
        f"{len(entities)} entities."
    )

    # finally:
    #     driver.close()


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
        create_entity_embeddings(driver, database, embedder)
    finally:
        driver.close()