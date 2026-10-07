from neo4j_graphrag.retrievers import VectorRetriever
from neo4j_graphrag.types import RetrieverResultItem

COMMUNITY_INDEX_NAME = "community_vector_index"
ENTITY_INDEX_NAME = "entity_vector_index"
CHUNK_INDEX_NAME = "chunk_vector_index"


def entity_result_formatter(record):
    node = record["node"]
    metadata = {
        "canonical_id": node.get("id"),
        "label": node.get("label"),
        "name": node.get("name"),
        "score": record["score"],
    }
    if "repository" in node:
        metadata["repository"] = node.get("repository")
    return RetrieverResultItem(
        content=node["name"],
        metadata=metadata,
    )


def community_result_formatter(record):
    node = record["node"]
    return RetrieverResultItem(
        content=node["summary"],
        metadata={
            "community_id": node.get("communityId"),
            "repository": node.get("repository"),
            "score": record["score"],
        },
    )


def chunk_result_formatter(record):
    node = record["node"]
    # Keep the complete chunk as the retrieval evidence. The answer layer can
    # display the excerpt directly, while file_path/chunk_id make it traceable.
    return RetrieverResultItem(
        content=node.get("text") or node.get("content") or "",
        metadata={
            "chunk_id": node.get("id"),
            "repository": node.get("repository"),
            "commit": node.get("commit"),
            "file_path": node.get("filePath"),
            "chunk_index": node.get("chunkIndex"),
            "content_hash": node.get("contentHash"),
            "strategy": node.get("strategy"),
            "score": record["score"],
        },
    )


def create_retrievers(driver, database: str | None = None):
    """Build entity, community, and source-code chunk vector retrievers."""
    entity_retriever = VectorRetriever(
        driver=driver,
        index_name=ENTITY_INDEX_NAME,
        return_properties=["id", "label", "name", "repository"],
        result_formatter=entity_result_formatter,
        neo4j_database=database,
    )

    community_retriever = VectorRetriever(
        driver=driver,
        index_name=COMMUNITY_INDEX_NAME,
        return_properties=["communityId", "summary", "repository"],
        result_formatter=community_result_formatter,
        neo4j_database=database,
    )

    chunk_retriever = VectorRetriever(
        driver=driver,
        index_name=CHUNK_INDEX_NAME,
        return_properties=[
            "id",
            "repository",
            "commit",
            "filePath",
            "chunkIndex",
            "text",
            "content",
            "contentHash",
            "strategy",
        ],
        result_formatter=chunk_result_formatter,
        neo4j_database=database,
    )

    return entity_retriever, community_retriever, chunk_retriever
