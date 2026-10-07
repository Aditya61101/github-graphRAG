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
