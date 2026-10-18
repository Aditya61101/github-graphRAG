from __future__ import annotations

from collections import defaultdict
import re
from shared.ai.space import current_space, vector_metadata


_REL_TYPE_RE = re.compile(r"[^A-Z0-9_]")


def sanitize_relationship_type(value: str) -> str:
    """Return a valid, non-empty Neo4j relationship type."""
    rel_type = _REL_TYPE_RE.sub("_", value.strip().upper())
    rel_type = rel_type.strip("_")
    if not rel_type:
        return "RELATED_TO"
    if rel_type[0].isdigit():
        return f"R_{rel_type}"
    return rel_type


class Neo4jRepositoryWriter:
    """Direct Neo4j writer with explicit evidence provenance and repository scoping."""

    def __init__(self, driver, database="neo4j"):
        self.driver, self.database = driver, database
        self.embedding_dimensions = 3072

    def initialize_constraints(self, *, embedding_dimensions: int = 3072):
        if embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive.")
        self.embedding_dimensions = embedding_dimensions

        for q in [
            "CREATE CONSTRAINT repository_key IF NOT EXISTS FOR (n:Repository) REQUIRE n.key IS UNIQUE",
            "CREATE CONSTRAINT file_key IF NOT EXISTS FOR (n:File) REQUIRE n.key IS UNIQUE",
            "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (n:Chunk) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT assertion_id IF NOT EXISTS FOR (n:GraphAssertion) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT community_id IF NOT EXISTS FOR (n:Community) REQUIRE n.communityId IS UNIQUE",
            "CREATE INDEX entity_repo_idx IF NOT EXISTS FOR (n:Entity) ON (n.repository)",
            "CREATE INDEX chunk_repo_idx IF NOT EXISTS FOR (n:Chunk) ON (n.repository)",
            "CREATE INDEX file_repo_idx IF NOT EXISTS FOR (n:File) ON (n.repository)",
            "CREATE INDEX assertion_repo_idx IF NOT EXISTS FOR (n:GraphAssertion) ON (n.repository)",
            "CREATE INDEX community_repo_idx IF NOT EXISTS FOR (n:Community) ON (n.repository)",
        ]:
            try:
                self.driver.execute_query(q, database_=self.database)
            except Exception as exc:
                raise RuntimeError("Required repository graph schema initialization failed") from exc

        index_options = (
            "{ indexConfig: { "
            f"`vector.dimensions`: {embedding_dimensions}, "
            "`vector.similarity_function`: 'cosine' "
            "} }"
        )
        for query in [
            "CREATE VECTOR INDEX chunk_vector_index IF NOT EXISTS "
            "FOR (n:Chunk) ON n.embedding "
            f"OPTIONS {index_options}",
            "CREATE VECTOR INDEX entity_vector_index IF NOT EXISTS "
            "FOR (n:Entity) ON n.embedding "
            f"OPTIONS {index_options}",
        ]:
            try:
                self.driver.execute_query(query, database_=self.database)
            except Exception as exc:
                raise RuntimeError("Required repository vector index initialization failed") from exc

    def write_source(
        self,
        *,
        repository: str,
        commit: str,
        chunks,
        full_name: str | None = None,
        owner: str | None = None,
        name: str | None = None,
    ):
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        chunks = list(chunks)
        for chunk in chunks:
            if chunk.embedding is None:
                raise ValueError(f"Chunk {chunk.chunk_id} is missing its embedding.")
            if len(chunk.embedding) != self.embedding_dimensions:
                raise ValueError(
                    f"Chunk embedding for {chunk.chunk_id} has dimension "
                    f"{len(chunk.embedding)}, expected {self.embedding_dimensions}."
                )
        self.driver.execute_query(
            """
            MERGE (r:Repository {key: $repo})
            SET r.github_repository_id = $repo,
                r.full_name = coalesce($full_name, r.full_name),
                r.owner = coalesce($owner, r.owner),
                r.name = coalesce($name, r.name)
            MERGE (c:Commit {key: $commit})
            MERGE (r)-[:HAS_COMMIT]->(c)
            WITH c, r
            UNWIND $chunks AS item
            MERGE (f:File {key: item.file_key})
            SET f.path = item.path,
                f.repository = item.repository,
                f.commit = item.commit
            MERGE (c)-[:CONTAINS]->(f)
            MERGE (r)-[:HAS_FILE]->(f)
            MERGE (ch:Chunk {id: item.id})
            SET ch.repository = item.repository,
                ch.commit = item.commit,
                ch.filePath = item.path,
                ch.chunkIndex = item.chunk_index,
                ch.strategy = item.strategy,
                ch.contentHash = item.content_hash,
                ch.text = item.text,
                ch.embedding = item.embedding,
                ch.embeddingSpace = item.embedding_space
            SET ch += item.vector_metadata
            MERGE (f)-[:HAS_CHUNK]->(ch)
            """,
            repo=repository,
            commit=commit,
            full_name=full_name,
            owner=owner,
            name=name,
            chunks=[
                {
                    "id": c.chunk_id,
                    "repository": repository,
                    "commit": c.commit,
                    "path": c.file_path,
                    "file_key": f"{repository}:{c.file_path}",
                    "chunk_index": c.chunk_index,
                    "strategy": c.strategy.value,
                    "content_hash": c.content_hash,
                    "text": c.text,
                    "embedding": c.embedding,
                    "embedding_space": current_space(),
                    "vector_metadata": vector_metadata(c.text),
                }
                for c in chunks
            ],
            database_=self.database,
        )

    def write_entities(self, entities, repository: str | None = None):
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        payload = []
        for entity in entities:
            if not isinstance(entity.name, str) or not entity.name.strip():
                continue

            properties = {
                key: value
                for key, value in dict(entity.properties).items()
                if key not in {"id", "name", "label", "aliases", "repository"}
                and not key.startswith("embedding")
                and value is not None
            }
            if entity.embedding is None:
                raise ValueError(
                    f"Entity {entity.canonical_id} is missing its embedding."
                )
            if len(entity.embedding) != self.embedding_dimensions:
                raise ValueError(
                    f"Entity embedding for {entity.canonical_id} has dimension "
                    f"{len(entity.embedding)}, expected {self.embedding_dimensions}."
                )

            entity_props = {
                **properties,
                "id": entity.canonical_id,
                "name": entity.name.strip(),
                "label": entity.label,
                "aliases": list(entity.aliases),
                "embedding": [float(value) for value in entity.embedding],
                **vector_metadata(),
            }
            if repository:
                entity_props["repository"] = repository

            payload.append(
                {
                    "id": entity.canonical_id,
                    "properties": entity_props,
                    "evidence": list(dict.fromkeys(entity.evidence_chunk_ids)),
                }
            )

        if not payload:
            return

        self.driver.execute_query(
            """
            UNWIND $entities AS item
            MERGE (e:Entity {id: item.id})
            WITH e, item, EXISTS {
                MATCH (:Chunk)-[:MENTIONS]->(e)
            } AS surviving_support
            FOREACH (_ IN CASE WHEN surviving_support THEN [] ELSE [1] END |
                SET e = item.properties
            )
            FOREACH (_ IN CASE WHEN surviving_support THEN [1] ELSE [] END |
                SET e.aliases = reduce(names=coalesce(e.aliases,[]), name IN item.properties.aliases |
                    CASE WHEN name IN names THEN names ELSE names+[name] END)
            )
            WITH e, item
            FOREACH (_ IN CASE WHEN $repo IS NOT NULL THEN [1] ELSE [] END |
                SET e.repository = $repo
            )
            WITH e, item
            UNWIND item.evidence AS chunk_id
            MATCH (c:Chunk {id: chunk_id})
            MERGE (c)-[:MENTIONS]->(e)
            """,
            entities=payload,
            repo=repository,
            database_=self.database,
        )

    def write_relationships(self, relationships, repository: str | None = None):
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        relationships = list(relationships)
        if not relationships:
            return

        self.driver.execute_query(
            """
            UNWIND $relationships AS item
            MATCH (s:Entity {id: item.source_id})
            MATCH (t:Entity {id: item.target_id})

            MERGE (a:GraphAssertion {
                id: item.source_id+'|'+item.relationship_type+'|'+item.target_id
            })

            SET a = item.properties
            FOREACH (_ IN CASE WHEN $repo IS NOT NULL THEN [1] ELSE [] END |
                SET a.repository = $repo
            )

            MERGE (s)-[:ASSERTS]->(a)
            MERGE (a)-[:TARGETS]->(t)

            WITH a, item
            UNWIND item.evidence AS chunk_id
            MATCH (c:Chunk {id: chunk_id})
            MERGE (c)-[:SUPPORTS]->(a)
            """,
            relationships=[
                {
                    "source_id": r.source_id,
                    "target_id": r.target_id,
                    "relationship_type": sanitize_relationship_type(
                        r.relationship_type
                    ),
                    "properties": {
                        "id": (
                            f"{r.source_id}|"
                            f"{sanitize_relationship_type(r.relationship_type)}|"
                            f"{r.target_id}"
                        ),
                        "relationshipType": sanitize_relationship_type(
                            r.relationship_type
                        ),
                        "confidence": r.confidence,
                        "rationale": r.rationale,
                        **({"repository": repository} if repository else {}),
                        **dict(r.properties),
                    },
                    "evidence": list(dict.fromkeys(r.evidence_chunk_ids)),
                }
                for r in relationships
            ],
            repo=repository,
            database_=self.database,
        )

        grouped = defaultdict(list)
        for r in relationships:
            grouped[sanitize_relationship_type(r.relationship_type)].append(r)

        for rel_type, group in grouped.items():
            self.driver.execute_query(
                f"""
                UNWIND $relationships AS item
                MATCH (s:Entity {{id: item.source_id}})
                MATCH (t:Entity {{id: item.target_id}})
                MERGE (s)-[r:{rel_type}]->(t)
                SET r = item.properties
                FOREACH (_ IN CASE WHEN $repo IS NOT NULL THEN [1] ELSE [] END |
                    SET r.repository = $repo
                )
                """,
                relationships=[
                    {
                        "source_id": r.source_id,
                        "target_id": r.target_id,
                        "properties": {
                            "confidence": r.confidence,
                            **({"repository": repository} if repository else {}),
                            **dict(r.properties),
                        },
                    }
                    for r in group
                ],
                repo=repository,
                database_=self.database,
            )

    def delete_files(self, repository: str, file_paths: list[str]) -> None:
        """Remove stale files, their chunks, and orphan assertions/entities scoped to repository."""
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        if not file_paths:
            return

        file_keys = [f"{repository}:{path}" for path in file_paths]

        self.driver.execute_query("""
            MATCH (f:File {repository:$repo}) WHERE f.key IN $file_keys
            OPTIONAL MATCH (f)-[:HAS_CHUNK]->(ch:Chunk {repository:$repo})
            WHERE NOT ch:ADRChunk AND ch.adr_id IS NULL
            WITH collect(DISTINCT f) AS files, collect(DISTINCT ch) AS chunks
            FOREACH (c IN chunks | DETACH DELETE c)
            FOREACH (f IN files | DETACH DELETE f)
            """, file_keys=file_keys, repo=repository, database_=self.database)
        self.cleanup_unsupported(repository)

    def cleanup_unsupported(self, repository):
        # Independent queries: an empty assertion set must not suppress entity cleanup.
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        for query in [
            """MATCH (a:GraphAssertion {repository:$repo})
               WHERE coalesce(a.source_type,'code') <> 'ADR'
                 AND NOT EXISTS { MATCH (:Chunk)-[:SUPPORTS]->(a) }
               DETACH DELETE a""",
            """MATCH (s:Entity {repository:$repo})-[r]->(t:Entity {repository:$repo})
               WHERE coalesce(r.source_type,'code') <> 'ADR'
                 AND NOT EXISTS {
                   MATCH (s)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(t)
                   WHERE a.relationshipType=type(r)
                     AND (a.source_type='ADR' OR EXISTS { MATCH (:Chunk)-[:SUPPORTS]->(a) })
                 }
               DELETE r""",
            """MATCH (e:Entity {repository:$repo})
               WHERE NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }
                 AND NOT EXISTS {
                   MATCH (n)-[:AFFECTS|CONSTRAINS]->(e)
                   WHERE n.repository=$repo
                 }
                 AND NOT EXISTS {
                   MATCH (e)-[:ASSERTS|TARGETS]-(a:GraphAssertion)
                   WHERE a.source_type='ADR' OR EXISTS { MATCH (:Chunk)-[:SUPPORTS]->(a) }
                 }
               DETACH DELETE e""",
        ]:
            self.driver.execute_query(query, repo=repository, database_=self.database)

    def reconcile_code(self, repository):
        """Idempotent code-only repair; retain ADR nodes, support and applicability."""
        from ai_services.ingestion.graph_control import require_fenced_writer
        require_fenced_writer(self.driver)
        self.driver.execute_query("""
            MATCH (ch:Chunk {repository:$repo})
            WHERE NOT ch:ADRChunk AND ch.adr_id IS NULL
            DETACH DELETE ch
            """, repo=repository, database_=self.database)
        self.driver.execute_query("MATCH (f:File {repository:$repo}) DETACH DELETE f",
                                  repo=repository, database_=self.database)
        self.cleanup_unsupported(repository)

    def validate(self, repository):
        rows = self.driver.execute_query("""
            MATCH (a:GraphAssertion {repository:$repo})
            WHERE coalesce(a.source_type,'code') <> 'ADR'
              AND (NOT EXISTS { MATCH (:Chunk)-[:SUPPORTS]->(a) }
                OR NOT EXISTS { MATCH (:Entity)-[:ASSERTS]->(a)-[:TARGETS]->(:Entity) })
            RETURN count(a) AS invalid
            """, repo=repository, database_=self.database).records
        if len(rows) != 1 or rows[0]["invalid"]:
            raise RuntimeError("Repository graph provenance validation failed")
        rows = self.driver.execute_query("""
            MATCH (c:Community {repository:$repo})
            WHERE c.summary IS NULL OR c.summaryHash IS NULL OR c.contextHash IS NULL
               OR c.embedding IS NULL OR c.embeddedSummaryHash IS NULL
            RETURN count(c) AS invalid
            """, repo=repository, database_=self.database).records
        if len(rows) != 1 or rows[0]["invalid"]:
            raise RuntimeError("Repository community validation failed")
