from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GraphRepositoryError(Exception):
    """Raised when an error occurs during Neo4j graph query execution."""
    pass


class Neo4jGraphRepository:
    """Read-only Neo4j query repository for architectural entity and relationship data."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def _extract_records(self, result: Any) -> list[dict[str, Any]]:
        """Normalize execute_query results across official Neo4j driver and mock drivers."""
        if result is None:
            return []
        if hasattr(result, "records"):
            raw_records = result.records
        elif isinstance(result, (tuple, list)) and len(result) >= 1 and isinstance(result[0], list):
            raw_records = result[0]
        elif isinstance(result, list):
            raw_records = result
        else:
            raw_records = []

        normalized: list[dict[str, Any]] = []
        for r in raw_records:
            if isinstance(r, dict):
                normalized.append(r)
            elif hasattr(r, "data"):
                normalized.append(r.data())
            elif hasattr(r, "keys"):
                normalized.append(dict(r))
            else:
                normalized.append(dict(r))
        return normalized

    def fetch_entities(self, repo_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch canonical architectural Entity nodes strictly belonging to the specified repositories."""
        if not self.driver:
            raise GraphRepositoryError("Neo4j driver is not initialized.")
        if not repo_ids:
            return []

        query = """
        MATCH (e:Entity)
        WHERE e.repository IN $repo_ids
          AND e.id IS NOT NULL
        RETURN
            e.id AS id,
            e.name AS name,
            e.label AS label,
            e.aliases AS aliases,
            e.description AS description,
            e.source AS source,
            e.repository AS repository
        ORDER BY e.name, e.id
        """
        try:
            result = self.driver.execute_query(
                query,
                database_=self.database,
                repo_ids=repo_ids,
            )
            return self._extract_records(result)
        except Exception as exc:
            logger.exception(f"Failed to query entities from Neo4j for repositories {repo_ids}: {exc}")
            raise GraphRepositoryError(f"Failed to query entities from Neo4j: {exc}") from exc

    def count_architectural_decisions(self, repo_ids: list[str]) -> int:
        """Count persisted decision nodes within a server-authorized scope."""
        if not self.driver or not repo_ids:
            raise GraphRepositoryError("An initialized driver and repository scope are required.")
        query = """
        MATCH (d:ArchitecturalDecision)
        WHERE d.repository IN $repo_ids
        RETURN count(DISTINCT d) AS decision_count
        """
        try:
            records = self._extract_records(self.driver.execute_query(
                query, database_=self.database, repo_ids=repo_ids,
            ))
            if len(records) != 1:
                raise ValueError("Decision aggregate did not return one record")
            count = records[0].get('decision_count')
            if type(count) is not int or count < 0:
                raise ValueError("Decision aggregate returned an invalid count")
            return count
        except Exception as exc:
            logger.exception("Failed to count repository architectural decisions")
            raise GraphRepositoryError("Decision count query failed") from exc

    def _knowledge_query(self, query: str, repo_ids: list[str], **params):
        if not self.driver or not repo_ids:
            raise GraphRepositoryError('An initialized driver and authorized scope are required')
        try:
            return self._extract_records(self.driver.execute_query(
                query, database_=self.database, repo_ids=repo_ids, **params,
            ))
        except Exception as exc:
            logger.exception('Architectural knowledge query failed')
            raise GraphRepositoryError('Architectural knowledge could not be retrieved') from exc

    def list_architectural_knowledge(self, repo_ids: list[str], search: str, kind: str, offset: int, limit: int):
        scope = """
        MATCH (d)
        WHERE (d:ArchitecturalDecision OR d:ArchitecturalConstraint)
          AND d.repository IN $repo_ids AND d.id IS NOT NULL
          AND ($kind = 'all' OR ($kind = 'decision' AND d:ArchitecturalDecision)
               OR ($kind = 'constraint' AND d:ArchitecturalConstraint))
          AND (toLower(coalesce(d.title, '')) CONTAINS $search
               OR toLower(coalesce(d.description, '')) CONTAINS $search)
          AND EXISTS { MATCH (a:ADR)-[:DEFINES]->(d) WHERE a.repository IN $repo_ids }
        """
        params = dict(search=search.strip().lower(), kind=kind)
        totals = self._knowledge_query(scope + ' RETURN count(DISTINCT d) AS total', repo_ids, **params)
        if len(totals) != 1 or type(totals[0].get('total')) is not int:
            raise GraphRepositoryError('Invalid architectural knowledge aggregate')
        query = scope + """
        WITH d ORDER BY d.id SKIP $offset LIMIT $limit
        OPTIONAL MATCH (a:ADR)-[:DEFINES]->(d) WHERE a.repository IN $repo_ids
        RETURN d.id AS id, CASE WHEN d:ArchitecturalDecision THEN 'decision' ELSE 'constraint' END AS kind,
               d.title AS title, coalesce(d.description, '') AS description,
               coalesce(d.decision_type, d.constraint_type) AS type,
               collect(DISTINCT a { .id, .title, .source_name, .source_type, .source_version, .content_hash }) AS sources
        ORDER BY id
        """
        items = self._knowledge_query(query, repo_ids, **params, offset=offset, limit=limit)
        return dict(items=items, total=totals[0]['total'], offset=offset, limit=limit)

    def get_architectural_knowledge(self, repo_ids: list[str], item_id: str):
        query = """
        MATCH (d) WHERE d.id = $item_id AND d.repository IN $repo_ids
          AND (d:ArchitecturalDecision OR d:ArchitecturalConstraint)
        MATCH (a:ADR)-[:DEFINES]->(d) WHERE a.repository IN $repo_ids
        RETURN d.id AS id, CASE WHEN d:ArchitecturalDecision THEN 'decision' ELSE 'constraint' END AS kind,
               d.title AS title, coalesce(d.description, '') AS description,
               coalesce(d.decision_type, d.constraint_type) AS type,
               collect(DISTINCT a { .id, .title, .source_name, .source_type, .source_version, .content_hash }) AS sources
        """
        records = self._knowledge_query(query, repo_ids, item_id=item_id)
        if not records:
            return None
        detail = records[0]
        entities = self._knowledge_query("""
        MATCH (d)-[r:AFFECTS|CONSTRAINS]->(e:Entity)
        WHERE d.id = $item_id AND d.repository IN $repo_ids AND e.repository IN $repo_ids
          AND (d:ArchitecturalDecision OR d:ArchitecturalConstraint)
        RETURN DISTINCT e.id AS id, e.name AS name, e.label AS label, type(r) AS relationship
        ORDER BY id, relationship LIMIT 101
        """, repo_ids, item_id=item_id)
        evidence = self._knowledge_query("""
        MATCH (a:ADR)-[:DEFINES]->(d)<-[:SUPPORTS]-(c:Chunk:ADRChunk)
        MATCH (a)-[:HAS_CHUNK]->(c)
        WHERE d.id = $item_id AND d.repository IN $repo_ids AND a.repository IN $repo_ids
          AND c.repository IN $repo_ids AND c.adr_id = a.id
          AND (d:ArchitecturalDecision OR d:ArchitecturalConstraint)
        RETURN DISTINCT c.id AS id, a.id AS adr_id, c.section AS section,
               c.chunkIndex AS chunk_index, substring(coalesce(c.text, ''), 0, 1200) AS excerpt,
               size(coalesce(c.text, '')) > 1200 AS truncated, c.contentHash AS content_hash
        ORDER BY adr_id, chunk_index, id LIMIT 51
        """, repo_ids, item_id=item_id)
        detail.update(entities=entities[:100], entities_truncated=len(entities) > 100,
                      evidence=evidence[:50], evidence_truncated=len(evidence) > 50)
        return detail

    def fetch_direct_relationships(self, repo_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch direct Entity-to-Entity architectural relationships strictly within the specified repositories."""
        if not self.driver:
            raise GraphRepositoryError("Neo4j driver is not initialized.")
        if not repo_ids:
            return []

        query = """
        MATCH (s:Entity)-[r]->(t:Entity)
        WHERE s.repository IN $repo_ids
          AND t.repository IN $repo_ids
          AND s.id IS NOT NULL AND t.id IS NOT NULL AND s.id <> t.id
          AND NOT type(r) IN ["ASSERTS", "TARGETS", "MENTIONS", "MEMBER_OF"]
        RETURN
            s.id AS source_id,
            t.id AS target_id,
            type(r) AS rel_type,
            r.confidence AS confidence,
            r.rationale AS rationale,
            coalesce(r.source, 'code') AS source_type
        ORDER BY s.id, rel_type, t.id
        """
        try:
            result = self.driver.execute_query(
                query,
                database_=self.database,
                repo_ids=repo_ids,
            )
            return self._extract_records(result)
        except Exception as exc:
            logger.exception(f"Failed to query direct relationships from Neo4j for repositories {repo_ids}: {exc}")
            raise GraphRepositoryError(f"Failed to query direct relationships from Neo4j: {exc}") from exc

    def fetch_assertion_relationships(self, repo_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch GraphAssertion-backed relationships (e.g. ADR assertions) strictly within the specified repositories."""
        if not self.driver:
            raise GraphRepositoryError("Neo4j driver is not initialized.")
        if not repo_ids:
            return []

        query = """
        MATCH (s:Entity)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(t:Entity)
        WHERE s.repository IN $repo_ids
          AND t.repository IN $repo_ids
          AND a.repository IN $repo_ids
          AND s.id IS NOT NULL AND t.id IS NOT NULL AND s.id <> t.id
          AND a.relationshipType IS NOT NULL
        RETURN
            s.id AS source_id,
            t.id AS target_id,
            a.relationshipType AS rel_type,
            a.confidence AS confidence,
            a.rationale AS rationale,
            coalesce(a.source_type, 'assertion') AS source_type
        ORDER BY s.id, rel_type, t.id
        """
        try:
            result = self.driver.execute_query(
                query,
                database_=self.database,
                repo_ids=repo_ids,
            )
            return self._extract_records(result)
        except Exception as exc:
            logger.exception(f"Failed to query assertion relationships from Neo4j for repositories {repo_ids}: {exc}")
            raise GraphRepositoryError(f"Failed to query assertion relationships from Neo4j: {exc}") from exc
