from __future__ import annotations

import re
from collections import defaultdict


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
    """Direct Neo4j writer with explicit evidence provenance."""

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
        ]:
            self.driver.execute_query(q, database_=self.database)

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
            self.driver.execute_query(query, database_=self.database)

    def write_source(self, *, repository, commit, chunks):
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
            MERGE (r:Repository {key:$repo})
            MERGE (c:Commit {key:$commit})
            MERGE (r)-[:HAS_COMMIT]->(c)
            WITH c
            UNWIND $chunks AS item
            MERGE (f:File {key:item.file_key})
            SET f.path=item.path, f.repository=item.repository, f.commit=item.commit
            MERGE (c)-[:CONTAINS]->(f)
            MERGE (ch:Chunk {id:item.id})
            SET ch.repository=item.repository, ch.commit=item.commit,
                ch.filePath=item.path, ch.chunkIndex=item.chunk_index,
                ch.strategy=item.strategy, ch.contentHash=item.content_hash,
                ch.text=item.text, ch.embedding=item.embedding
            MERGE (f)-[:HAS_CHUNK]->(ch)
            """,
            repo=repository,
            commit=commit,
            chunks=[
                {
                    "id": c.chunk_id,
                    "repository": c.repository,
                    "commit": c.commit,
                    "path": c.file_path,
                    "file_key": f"{repository}:{c.file_path}",
                    "chunk_index": c.chunk_index,
                    "strategy": c.strategy.value,
                    "content_hash": c.content_hash,
                    "text": c.text,
                    "embedding": c.embedding,
                }
                for c in chunks
            ],
            database_=self.database,
        )

    def write_entities(self, entities):
        payload = []
        for entity in entities:
            # This is the final boundary before Neo4j. Canonicalization rejects
            # malformed identities, but cached or externally constructed models
            # must not be allowed to create nameless graph nodes either.
            if not isinstance(entity.name, str) or not entity.name.strip():
                continue

            # Extracted properties are descriptive only; they must never be
            # allowed to replace the canonical identity fields.
            properties = {
                key: value
                for key, value in dict(entity.properties).items()
                if key not in {"id", "name", "label", "aliases"}
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

            payload.append(
                {
                    "id": entity.canonical_id,
                    "properties": {
                        **properties,
                        "id": entity.canonical_id,
                        "name": entity.name.strip(),
                        "label": entity.label,
                        "aliases": list(entity.aliases),
                        "embedding": [float(value) for value in entity.embedding],
                    },
                    "evidence": list(dict.fromkeys(entity.evidence_chunk_ids)),
                }
            )

        if not payload:
            return

        self.driver.execute_query(
            """
            UNWIND $entities AS item
            MERGE (e:Entity {id:item.id})
            SET e = item.properties
            WITH e, item
            UNWIND item.evidence AS chunk_id
            MATCH (c:Chunk {id:chunk_id})
            MERGE (c)-[:MENTIONS]->(e)
            """,
            entities=payload,
            database_=self.database,
        )

    def write_relationships(self, relationships):
        relationships = list(relationships)
        if not relationships:
            return

        # Persist provenance/assertions in one query.
        self.driver.execute_query(
            """
            UNWIND $relationships AS item
            MATCH (s:Entity {id:item.source_id})
            MATCH (t:Entity {id:item.target_id})

            MERGE (a:GraphAssertion {
                id:item.source_id+'|'+item.relationship_type+'|'+item.target_id
            })

            SET a = item.properties

            MERGE (s)-[:ASSERTS]->(a)
            MERGE (a)-[:TARGETS]->(t)

            WITH a, item
            UNWIND item.evidence AS chunk_id
            MATCH (c:Chunk {id:chunk_id})
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
                        **dict(r.properties),
                    },
                    "evidence": list(dict.fromkeys(r.evidence_chunk_ids)),
                }
                for r in relationships
            ],
            database_=self.database,
        )

        # Dynamic relationship types cannot be passed as Cypher parameters, so
        # group by sanitized type and batch one UNWIND query per distinct type.
        grouped = defaultdict(list)
        for r in relationships:
            grouped[sanitize_relationship_type(r.relationship_type)].append(r)

        for rel_type, group in grouped.items():
            self.driver.execute_query(
                f"""
                UNWIND $relationships AS item
                MATCH (s:Entity {{id:item.source_id}})
                MATCH (t:Entity {{id:item.target_id}})
                MERGE (s)-[r:{rel_type}]->(t)
                SET r = item.properties
                """,
                relationships=[
                    {
                        "source_id": r.source_id,
                        "target_id": r.target_id,
                        "properties": {
                            "confidence": r.confidence,
                            **dict(r.properties),
                        },
                    }
                    for r in group
                ],
                database_=self.database,
            )
