from github_graphrag.query_entities import entity_result_formatter
from neo4j_graphrag.retrievers import VectorRetriever
from neo4j_graphrag.types import RetrieverResultItem

COMMUNITY_INDEX_NAME = "community_vector_index"
EMBEDDING_PROPERTY = "embedding"
EMBEDDING_DIMENSIONS = 768
ENTITY_INDEX_NAME = "entity_vector_index"

def community_result_formatter(record):
    node = record["node"]

    return RetrieverResultItem(
        content=node["summary"],
        metadata={
            "community_id": node["communityId"],
            "score": record["score"],
        },
    )

def create_retrievers(
    driver,
    embedder,
):
    entity_retriever = VectorRetriever(
        driver=driver,
        index_name=ENTITY_INDEX_NAME,
        embedder=embedder,
        return_properties=["name"],
        result_formatter=entity_result_formatter,
    )

    community_retriever = VectorRetriever(
        driver=driver,
        index_name=COMMUNITY_INDEX_NAME,
        embedder=embedder,
        return_properties=[
            "communityId",
            "summary",
        ],
        result_formatter=community_result_formatter,
    )

    return entity_retriever, community_retriever