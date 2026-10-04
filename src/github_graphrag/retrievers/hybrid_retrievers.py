from dataclasses import dataclass

from github_graphrag.graph_expansion import expand_entities
from github_graphrag.retrievers.context_formatter import format_retrieval_context


@dataclass(frozen=True)
class HybridRetrievalResult:
    context: str
    sources: dict[str, list[dict]]


def load_communities(
    driver,
    database: str,
    community_ids: list[str | int],
):
    if not community_ids:
        return []

    result = driver.execute_query(
        """
        MATCH (c:Community)
        WHERE c.communityId IN $community_ids

        CALL {
            WITH c
            OPTIONAL MATCH (e:Entity)-[:MEMBER_OF]->(c)
            RETURN collect(DISTINCT CASE WHEN e IS NULL THEN NULL ELSE {
                canonical_id: e.id,
                label: e.label,
                name: e.name
            } END) AS raw_members
        }
        CALL {
            WITH c
            OPTIONAL MATCH (source:Entity)-[:MEMBER_OF]->(c)
            OPTIONAL MATCH (source)-[r]->(target:Entity)-[:MEMBER_OF]->(c)
            WHERE r IS NULL OR type(r) <> "MEMBER_OF"
            RETURN collect(DISTINCT CASE WHEN r IS NULL THEN NULL ELSE {
                source_id: source.id,
                source_label: source.label,
                source_name: source.name,
                relationship: type(r),
                target_id: target.id,
                target_label: target.label,
                target_name: target.name
            } END) AS raw_relationships
        }

        RETURN
            c.communityId AS community_id,
            c.summary AS summary,
            [member IN raw_members WHERE member IS NOT NULL] AS members,
            [relationship IN raw_relationships WHERE relationship IS NOT NULL] AS relationships

        ORDER BY community_id
        """,
        community_ids=community_ids,
        database_=database,
    )

    return result.records


async def hybrid_retrieve(
    query: str,
    driver,
    database: str,
    embedder,
    entity_retriever,
    community_retriever,
    entity_top_k: int = 5,
    community_top_k: int = 3,
):
    vectors = await embedder.embed([query])
    if len(vectors) != 1:
        raise RuntimeError("Embedder must return exactly one vector for a query.")
    query_vector = vectors[0]

    # 1. Entity vector retrieval (canonical IDs are retained in metadata).

    entity_results = entity_retriever.search(
        query_vector=query_vector,
        top_k=entity_top_k,
    )

    entity_ids = [
        item.metadata["canonical_id"]
        for item in entity_results.items
        if item.metadata.get("canonical_id")
    ]

    # 2. One-hop graph expansion by canonical ID, preserving stored direction.

    graph_records = expand_entities(
        driver,
        entity_ids,
        database,
    )

    # 3. Independent community vector retrieval using the existing index.
    community_results = community_retriever.search(
        query_vector=query_vector,
        top_k=community_top_k,
    )
    community_ids = [
        item.metadata["community_id"]
        for item in community_results.items
    ]

    # 4. Load each matched community's persisted summary and graph context.
    communities = load_communities(
        driver,
        database,
        community_ids,
    )

    # 5. Combine the two independent retrieval paths into grounded context.
    context = format_retrieval_context(
        entity_results=entity_results,
        community_results=community_results,
        communities=communities,
        graph_records=graph_records,
    )

    return HybridRetrievalResult(
        context=context,
        sources={
            "entities": [
                {
                    "canonical_id": item.metadata.get("canonical_id"),
                    "label": item.metadata.get("label"),
                    "name": item.metadata.get("name", item.content),
                    "score": item.metadata.get("score"),
                }
                for item in entity_results.items
            ],
            "communities": [
                {
                    "community_id": item.metadata.get("community_id"),
                    "score": item.metadata.get("score"),
                }
                for item in community_results.items
            ],
        },
    )

if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv
    load_dotenv()

    from github_graphrag.graphdb_driver import CreateDriver
    from openai import AsyncAzureOpenAI
    from github_graphrag.embeddings.azure_openai import AzureOpenAIEmbedder
    from github_graphrag.retrievers.retriever_factory import create_retrievers

    async def main() -> None:
        uri = os.getenv("NEO4J_URI")
        username = os.getenv("NEO4J_USERNAME")
        password = os.getenv("NEO4J_PASSWORD")
        database = os.getenv("NEO4J_DATABASE") or None
        driver = CreateDriver(uri, username, password).get_driver()
        try:
            client = AsyncAzureOpenAI(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_key=os.environ["AZURE_OPENAI_API_KEY"],
                api_version=os.environ["AZURE_OPENAI_API_VERSION"],
            )
            embedder = AzureOpenAIEmbedder(
                client=client,
                deployment=os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"],
                dimensions=int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
            )
            entity_retriever, community_retriever = create_retrievers(
                driver=driver,
                database=database,
            )
            result = await hybrid_retrieve(
                query="Which components are responsible for user authentication?",
                driver=driver,
                database=database,
                embedder=embedder,
                entity_retriever=entity_retriever,
                community_retriever=community_retriever,
            )
            print("\nContext:\n", result.context)
        finally:
            driver.close()

    asyncio.run(main())
