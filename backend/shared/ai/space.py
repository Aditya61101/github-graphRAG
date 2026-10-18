"""Admit only complete Google vector spaces in an attested installation."""
from .provider import ProviderConfig
import hashlib


def current_space():
    config = ProviderConfig.from_env()
    return config.embedding_space if config.provider == "google" else "azure-legacy"


def vector_metadata(text=None):
    config = ProviderConfig.from_env()
    if config.provider != "google":
        return {}
    metadata = {"embeddingSpace": config.embedding_space,
        "embedding_provider": "google-vertex", "embedding_model": config.embedding_model,
        "embedding_dimensions": config.dimensions, "embedding_revision": config.embedding_space}
    from .embedding_inputs import EMBEDDING_INPUT_POLICY
    metadata["embeddingInputPolicy"] = EMBEDDING_INPUT_POLICY
    if text is not None:
        metadata["embedding_text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return metadata


def assert_vector_space(driver, database, repository_id, *, global_indexes=False):
    space = current_space()
    if space == "azure-legacy":
        return
    scope = "MATCH (n)" if global_indexes else "MATCH (n {repository:$repo})"
    rows = driver.execute_query(scope + """
        WHERE n:Chunk OR n:Entity OR n:Community
        RETURN count(n) AS expected, count(CASE WHEN n:Community THEN 1 END) AS communities,
          count(CASE WHEN n.embeddingSpace=$space AND n.embedding IS NOT NULL
            AND size(n.embedding)=3072 THEN 1 END) AS completed
        """, repo=repository_id, space=space, database_=database).records
    if len(rows) != 1 or rows[0]["expected"] != rows[0]["completed"]:
        raise ValueError("Google vector space incomplete or incompatible; fresh ingestion or compatible backfill required")
    indexes = driver.execute_query("""
        SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties, options
        WHERE name IN ['chunk_vector_index', 'entity_vector_index', 'community_vector_index']
        RETURN name, type, state, labelsOrTypes, properties, options
        """, database_=database).records
    actual = {row["name"]: dict(row) for row in indexes}
    expected = {"chunk_vector_index": "Chunk", "entity_vector_index": "Entity"}
    if rows[0]["communities"]:
        expected["community_vector_index"] = "Community"
    for name, label in expected.items():
        index = actual.get(name, {})
        options = index.get("options", {}).get("indexConfig", {})
        if (index.get("type") != "VECTOR" or index.get("state") != "ONLINE"
                or index.get("labelsOrTypes") != [label] or index.get("properties") != ["embedding"]
                or options.get("vector.dimensions") != 3072
                or str(options.get("vector.similarity_function", "")).lower() != "cosine"):
            raise ValueError("Google vector index is absent, incompatible or not ONLINE: " + name)
