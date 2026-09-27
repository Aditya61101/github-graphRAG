"""
Thin wrapper around the Neo4j driver.

All Cypher in the pipeline goes through `GraphStore.run()`. The only
Neo4j-specific pieces (constraints, vector indexes, vector search) live in
this file, so they are easy to find and easy to swap in tests.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

BATCH_SIZE = 500

# label -> vector index name
VECTOR_INDEXES = {
    "Function": "function_embedding",
    "Section": "section_embedding",
    "Module": "module_embedding",
}

CONSTRAINTS = [
    "CREATE CONSTRAINT repo_name IF NOT EXISTS FOR (n:Repo) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT file_id IF NOT EXISTS FOR (n:File) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT class_id IF NOT EXISTS FOR (n:Class) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT function_id IF NOT EXISTS FOR (n:Function) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (n:Doc) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT section_id IF NOT EXISTS FOR (n:Section) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT module_id IF NOT EXISTS FOR (n:Module) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT cache_key IF NOT EXISTS FOR (n:SummaryCache) REQUIRE n.key IS UNIQUE",
    "CREATE INDEX function_name IF NOT EXISTS FOR (n:Function) ON (n.name)",
    "CREATE INDEX function_repo IF NOT EXISTS FOR (n:Function) ON (n.repo)",
    "CREATE INDEX section_repo IF NOT EXISTS FOR (n:Section) ON (n.repo)",
]


class GraphStore:
    def __init__(self, driver, database: str | None = None):
        self.driver = driver
        self.database = database

    # ---------------------------------------------------------------- basics

    def run(self, query: str, **params) -> list[dict]:
        records, _, _ = self.driver.execute_query(
            query, parameters_=params, database_=self.database
        )
        return [dict(r) for r in records]

    def run_batched(self, query: str, rows: list[dict], size: int = BATCH_SIZE) -> None:
        for i in range(0, len(rows), size):
            self.run(query, rows=rows[i : i + size])

    # ---------------------------------------------------------------- schema

    def ensure_schema(self) -> None:
        for statement in CONSTRAINTS:
            self.run(statement)

    def ensure_vector_indexes(self, dims: int, embedder_name: str) -> None:
        """Create vector indexes once, and refuse to mix embedding models."""
        meta = self.run(
            "MATCH (m:GraphMeta {key: 'embedding'}) RETURN m.model AS model, m.dims AS dims"
        )
        if meta and (meta[0]["dims"] != dims or meta[0]["model"] != embedder_name):
            raise RuntimeError(
                f"The graph was embedded with {meta[0]['model']} ({meta[0]['dims']} dims) "
                f"but the current embedder is {embedder_name} ({dims} dims). "
                "Use the same EMBEDDING_PROVIDER, or run `codegraph reset-embeddings`."
            )
        for label, index in VECTOR_INDEXES.items():
            self.run(
                f"CREATE VECTOR INDEX {index} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.embedding) "
                "OPTIONS {indexConfig: {"
                f"`vector.dimensions`: {int(dims)}, "
                "`vector.similarity_function`: 'cosine'}}"
            )
        self.run(
            "MERGE (m:GraphMeta {key: 'embedding'}) SET m.model = $model, m.dims = $dims",
            model=embedder_name,
            dims=dims,
        )

    def drop_vector_indexes(self) -> None:
        for index in VECTOR_INDEXES.values():
            self.run(f"DROP INDEX {index} IF EXISTS")
        self.run("MATCH (m:GraphMeta {key: 'embedding'}) DELETE m")

    # ---------------------------------------------------------------- vectors

    def vector_search(
        self, label: str, vector: list[float], k: int, repo: str | None = None
    ) -> list[dict]:
        """Top-k nodes of `label` by cosine similarity: [{id, score}]."""
        # The index is global, so over-fetch and filter by repo afterwards.
        fetch = k * 5 if repo else k
        return self.run(
            """
            CALL db.index.vector.queryNodes($index, $fetch, $vector)
            YIELD node, score
            WHERE $repo IS NULL OR node.repo = $repo
            RETURN node.id AS id, score
            ORDER BY score DESC
            LIMIT $k
            """,
            index=VECTOR_INDEXES[label],
            fetch=fetch,
            vector=vector,
            repo=repo,
            k=k,
        )
