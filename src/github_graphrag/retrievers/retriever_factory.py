from neo4j_graphrag.retrievers import VectorRetriever
from neo4j_graphrag.types import RetrieverResultItem

COMMUNITY_INDEX_NAME = "community_vector_index"
ENTITY_INDEX_NAME = "entity_vector_index"


def entity_result_formatter(record):
    node = record["node"]

    return RetrieverResultItem(
        content=node["name"],
        metadata={
            "canonical_id": node["id"],
            "label": node["label"],
            "name": node["name"],
            "score": record["score"],
        },
    )


def community_result_formatter(record):
    node = record["node"]

    return RetrieverResultItem(
        content=node["summary"],
        metadata={
            "community_id": node["communityId"],
            "score": record["score"],
        },
    )

def create_retrievers(driver, database: str | None = None):
    """Build the existing independent entity and community vector retrievers.

    Query vectors are intentionally supplied by the caller. This lets the
    project-wide asynchronous ``Embedder`` adapter be reused directly instead
    of introducing a second provider-specific embedding implementation.
    """
    entity_retriever = VectorRetriever(
        driver=driver,
        index_name=ENTITY_INDEX_NAME,
        return_properties=["id", "label", "name"],
        result_formatter=entity_result_formatter,
        neo4j_database=database,
    )

    community_retriever = VectorRetriever(
        driver=driver,
        index_name=COMMUNITY_INDEX_NAME,
        return_properties=[
            "communityId",
            "summary",
        ],
        result_formatter=community_result_formatter,
        neo4j_database=database,
    )

    return entity_retriever, community_retriever
