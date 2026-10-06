from dotenv import load_dotenv
load_dotenv()


def expand_entities(
    driver,
    entity_ids: list[str],
    database: str | None,
    repository_id: str | None = None,
) -> list[dict]:
    """Return directed architectural edges adjacent to canonical IDs, filtered by repository if provided."""
    if not entity_ids:
        return []

    records, _, _ = driver.execute_query(
        """
        MATCH (source:Entity)-[r]->(target:Entity)
        WHERE (source.id IN $entity_ids OR target.id IN $entity_ids)
          AND ($repo_id IS NULL OR (source.repository = $repo_id AND target.repository = $repo_id))

        CALL {
            WITH source, target, r

            OPTIONAL MATCH (source)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(target)
            WHERE a.relationshipType = type(r)
              AND ($repo_id IS NULL OR a.repository = $repo_id)

            OPTIONAL MATCH (c:Chunk)-[:SUPPORTS]->(a)
            WHERE ($repo_id IS NULL OR c.repository = $repo_id)

            RETURN collect(DISTINCT c.id) AS assertion_chunk_ids
        }

        RETURN
            source.id AS source_id,
            source.label AS source_label,
            source.name AS source,
            type(r) AS relationship,
            target.id AS target_id,
            target.label AS target_label,
            target.name AS target,
            assertion_chunk_ids AS relationship_evidence_chunk_ids
        ORDER BY source, relationship, target
        """,
        entity_ids=list(dict.fromkeys(entity_ids)),
        repo_id=repository_id,
        database_=database,
    )

    return [dict(record) for record in records]


def load_entity_evidence_ids(
    driver,
    entity_ids: list[str],
    database: str | None,
    repository_id: str | None = None,
) -> dict[str, list[str]]:
    """Resolve entity provenance to supporting Chunk IDs."""
    if not entity_ids:
        return {}

    result = driver.execute_query(
        """
        MATCH (e:Entity)
        WHERE e.id IN $entity_ids
          AND ($repo_id IS NULL OR e.repository = $repo_id)

        OPTIONAL MATCH (c1:Chunk)-[:MENTIONS]->(e)
        WHERE ($repo_id IS NULL OR c1.repository = $repo_id)

        OPTIONAL MATCH (c2:Chunk)-[:SUPPORTS]->(a:GraphAssertion)-[:ASSERTS]->(e)
        WHERE ($repo_id IS NULL OR c2.repository = $repo_id)

        RETURN
            e.id AS entity_id,
            collect(DISTINCT c1.id) +
            collect(DISTINCT c2.id) AS chunk_ids
        """,
        entity_ids=list(dict.fromkeys(entity_ids)),
        repo_id=repository_id,
        database_=database,
    )

    return {
        record["entity_id"]: list(
            dict.fromkeys(
                chunk_id
                for chunk_id in (record["chunk_ids"] or [])
                if chunk_id is not None
            )
        )
        for record in result.records
    }


def load_evidence_chunks(
    driver,
    chunk_ids: list[str],
    database: str | None,
    repository_id: str | None = None,
) -> list[dict]:
    """Load persisted EvidenceChunk records by stable chunk ID."""
    if not chunk_ids:
        return []

    result = driver.execute_query(
        """
        MATCH (c:Chunk)
        WHERE c.id IN $chunk_ids
          AND ($repo_id IS NULL OR c.repository = $repo_id)
        RETURN
            c.id AS chunk_id,
            c.repository AS repository,
            c.commit AS commit,
            c.filePath AS file_path,
            c.chunkIndex AS chunk_index,
            coalesce(c.text, '') AS text,
            c.contentHash AS content_hash,
            c.strategy AS strategy
        ORDER BY c.filePath, c.chunkIndex
        """,
        chunk_ids=list(dict.fromkeys(chunk_ids)),
        repo_id=repository_id,
        database_=database,
    )
    return [dict(record) for record in result.records]
