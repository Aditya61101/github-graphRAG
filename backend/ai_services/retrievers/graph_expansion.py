from __future__ import annotations

from typing import Any
from dotenv import load_dotenv

load_dotenv()


def _normalize_records(result: Any) -> list[dict]:
    if result is None:
        return []
    if hasattr(result, "records"):
        raw = result.records
    elif isinstance(result, (tuple, list)) and len(result) >= 1 and isinstance(result[0], list):
        raw = result[0]
    elif isinstance(result, list):
        raw = result
    else:
        raw = []
    return [dict(r) for r in raw]


def expand_entities(
    driver,
    entity_ids: list[str],
    database: str | None,
    repository_id: str | None = None,
) -> list[dict]:
    """Return directed architectural edges adjacent to canonical IDs, filtered by repository if provided.

    Unifies direct code relationships and assertion-backed (e.g. ADR) relationships.
    """
    if not entity_ids:
        return []

    res = driver.execute_query(
        """
        MATCH (source:Entity)-[r]->(target:Entity)
        WHERE (source.id IN $entity_ids OR target.id IN $entity_ids)
          AND ($repo_id IS NULL OR (source.repository = $repo_id AND target.repository = $repo_id))
          AND NOT type(r) IN ["ASSERTS", "TARGETS", "MENTIONS", "MEMBER_OF"]

        CALL {
            WITH source, target, r

            OPTIONAL MATCH (source)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(target)
            WHERE a.relationshipType = type(r)
              AND ($repo_id IS NULL OR a.repository = $repo_id)

            OPTIONAL MATCH (c:Chunk)-[:SUPPORTS]->(a)
            WHERE ($repo_id IS NULL OR c.repository = $repo_id)

            RETURN collect(DISTINCT c.id) AS assertion_chunk_ids,
                   collect(DISTINCT a.id) AS assertion_ids
        }

        RETURN
            source.id AS source_id,
            source.label AS source_label,
            source.name AS source,
            type(r) AS relationship,
            target.id AS target_id,
            target.label AS target_label,
            target.name AS target,
            assertion_chunk_ids AS relationship_evidence_chunk_ids,
            assertion_ids AS assertion_ids

        UNION

        MATCH (source:Entity)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(target:Entity)
        WHERE (source.id IN $entity_ids OR target.id IN $entity_ids)
          AND ($repo_id IS NULL OR (source.repository = $repo_id AND target.repository = $repo_id AND a.repository = $repo_id))
          AND source.id IS NOT NULL AND target.id IS NOT NULL AND source.id <> target.id
          AND a.relationshipType IS NOT NULL

        CALL {
            WITH a
            OPTIONAL MATCH (c:Chunk)-[:SUPPORTS]->(a)
            WHERE ($repo_id IS NULL OR c.repository = $repo_id)
            RETURN collect(DISTINCT c.id) AS assertion_chunk_ids
        }

        RETURN
            source.id AS source_id,
            source.label AS source_label,
            source.name AS source,
            a.relationshipType AS relationship,
            target.id AS target_id,
            target.label AS target_label,
            target.name AS target,
            assertion_chunk_ids AS relationship_evidence_chunk_ids,
            [a.id] AS assertion_ids
        """,
        entity_ids=list(dict.fromkeys(entity_ids)),
        repo_id=repository_id,
        database_=database,
    )

    raw_records = _normalize_records(res)

    merged: dict[tuple[str, str, str], dict] = {}
    for rec in raw_records:
        key = (str(rec["source_id"]), str(rec["relationship"]), str(rec["target_id"]))
        if key not in merged:
            merged[key] = {
                "source_id": rec["source_id"],
                "source_label": rec.get("source_label") or "Entity",
                "source": rec.get("source") or rec["source_id"],
                "relationship": rec["relationship"],
                "target_id": rec["target_id"],
                "target_label": rec.get("target_label") or "Entity",
                "target": rec.get("target") or rec["target_id"],
                "relationship_evidence_chunk_ids": list(rec.get("relationship_evidence_chunk_ids") or []),
                "assertion_ids": [aid for aid in (rec.get("assertion_ids") or []) if aid],
            }
        else:
            existing = merged[key]
            for cid in (rec.get("relationship_evidence_chunk_ids") or []):
                if cid and cid not in existing["relationship_evidence_chunk_ids"]:
                    existing["relationship_evidence_chunk_ids"].append(cid)
            for aid in (rec.get("assertion_ids") or []):
                if aid and aid not in existing["assertion_ids"]:
                    existing["assertion_ids"].append(aid)

    return sorted(merged.values(), key=lambda x: (x["source"], x["relationship"], x["target"]))


def load_chunk_graph_associations(
    driver, chunk_ids: list[str], database: str | None,
    repository_id: str | None = None,
) -> list[dict]:
    """Batch graph metadata supported directly by the selected chunk IDs."""
    if not chunk_ids:
        return []
    result = driver.execute_query(
        """
        MATCH (c:Chunk)
        WHERE c.id IN $chunk_ids AND ($repo_id IS NULL OR c.repository = $repo_id)
        CALL {
            WITH c
            OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
            WHERE e.id IS NOT NULL AND ($repo_id IS NULL OR e.repository = $repo_id)
            RETURN collect(DISTINCT e.id) AS entity_ids
        }
        CALL {
            WITH c
            OPTIONAL MATCH (c)-[:SUPPORTS]->(a:GraphAssertion)
            WHERE a.id IS NOT NULL AND ($repo_id IS NULL OR a.repository = $repo_id)
            OPTIONAL MATCH (s:Entity)-[:ASSERTS]->(a)-[:TARGETS]->(t:Entity)
            WHERE s.id IS NOT NULL AND t.id IS NOT NULL AND s.id <> t.id
              AND ($repo_id IS NULL OR (s.repository = $repo_id AND t.repository = $repo_id))
            RETURN collect(DISTINCT CASE WHEN a IS NULL THEN NULL ELSE {
                assertion_id: a.id, relationship: a.relationshipType,
                source_id: s.id, target_id: t.id
            } END) AS raw_assertion_records
        }
        CALL {
            WITH c
            OPTIONAL MATCH (s:Entity)-[r]->(t:Entity)
            WHERE c.id IN coalesce(r.evidence_chunk_ids, [])
              AND s.id IS NOT NULL AND t.id IS NOT NULL AND s.id <> t.id
              AND NOT type(r) IN ["ASSERTS", "TARGETS", "MENTIONS", "MEMBER_OF"]
              AND ($repo_id IS NULL OR (s.repository = $repo_id AND t.repository = $repo_id))
            RETURN collect(DISTINCT CASE WHEN r IS NULL THEN NULL ELSE {
                source_id: s.id, relationship: type(r), target_id: t.id
            } END) AS raw_direct_records
        }
        RETURN c.id AS chunk_id, c.repository AS repository, entity_ids,
               raw_assertion_records, raw_direct_records
        ORDER BY chunk_id
        """,
        chunk_ids=list(dict.fromkeys(chunk_ids)), repo_id=repository_id, database_=database,
    )
    return [
        {
            "chunk_id": row["chunk_id"], "repository": row.get("repository"),
            "entity_ids": [eid for eid in row.get("entity_ids", []) if eid],
            "assertion_records": [r for r in row.get("raw_assertion_records", []) if r and r.get("assertion_id")],
            "direct_records": [r for r in row.get("raw_direct_records", []) if r],
        }
        for row in _normalize_records(result)
    ]


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

        OPTIONAL MATCH (e)-[:ASSERTS|TARGETS]-(a:GraphAssertion)<-[:SUPPORTS]-(c2:Chunk)
        WHERE ($repo_id IS NULL OR (c2.repository = $repo_id AND a.repository = $repo_id))

        RETURN
            e.id AS entity_id,
            collect(DISTINCT c1.id) +
            collect(DISTINCT c2.id) AS chunk_ids
        """,
        entity_ids=list(dict.fromkeys(entity_ids)),
        repo_id=repository_id,
        database_=database,
    )

    records = _normalize_records(result)

    return {
        record["entity_id"]: list(
            dict.fromkeys(
                chunk_id
                for chunk_id in (record.get("chunk_ids") or [])
                if chunk_id is not None
            )
        )
        for record in records
        if "entity_id" in record
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
    return _normalize_records(result)


def load_signal_evidence_chunks(
    driver, entity_hits: list[dict], community_hits: list[dict],
    database: str | None, repository_id: str | None, limit: int,
) -> list[dict]:
    """Convert entity/community discovery signals to bounded source chunks.

    Both channels use existing MENTIONS or assertion SUPPORTS provenance;
    graph walks and community summaries never become final evidence.
    """
    if not entity_hits and not community_hits:
        return []
    result = driver.execute_query(
        """
        CALL {
            UNWIND $entity_hits AS hit
            MATCH (e:Entity {id: hit.id})
            WHERE $repo_id IS NULL OR e.repository = $repo_id
            RETURN e, hit, 'entity' AS channel
            UNION ALL
            UNWIND $community_hits AS hit
            MATCH (e:Entity)-[:MEMBER_OF]->(community:Community {communityId: hit.id})
            WHERE $repo_id IS NULL OR (e.repository = $repo_id AND community.repository = $repo_id)
            RETURN e, hit, 'community' AS channel
        }
        CALL {
            WITH e
            MATCH (ch:Chunk)-[:MENTIONS]->(e)
            WHERE $repo_id IS NULL OR ch.repository = $repo_id
            RETURN ch
            UNION
            WITH e
            MATCH (e)-[:ASSERTS|TARGETS]-(a:GraphAssertion)<-[:SUPPORTS]-(ch:Chunk)
            WHERE $repo_id IS NULL OR (ch.repository = $repo_id AND a.repository = $repo_id)
            RETURN ch
        }
        WITH ch, collect(DISTINCT {
            channel: channel, rank: hit.rank, score: hit.score, id: hit.id
        }) AS signals, min(hit.rank) AS best_rank, count(DISTINCT channel) AS channel_count
        ORDER BY channel_count DESC, best_rank, ch.id
        LIMIT $limit
        RETURN ch.id AS chunk_id, ch.repository AS repository, ch.commit AS commit,
               ch.filePath AS file_path, ch.chunkIndex AS chunk_index,
               coalesce(ch.text, ch.content, '') AS text,
               ch.contentHash AS content_hash, ch.strategy AS strategy, signals
        """,
        entity_hits=entity_hits, community_hits=community_hits,
        repo_id=repository_id, limit=limit, database_=database,
    )
    return _normalize_records(result)
