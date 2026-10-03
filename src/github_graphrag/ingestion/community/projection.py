"""Graph projection manager for Neo4j Graph Data Science (GDS)."""

from __future__ import annotations

import asyncio
from inspect import iscoroutinefunction
import logging
import re
from typing import Any

from .config import CommunityConfig
from .exceptions import CommunityProjectionError

logger = logging.getLogger(__name__)

_RELATIONSHIP_TYPE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _execute_query(
    driver: Any,
    query: str,
    database: str | None = None,
    **params: Any,
) -> Any:
    """Execute a Cypher query asynchronously, safely handling sync or async drivers."""
    if hasattr(driver, "execute_query"):
        if iscoroutinefunction(driver.execute_query):
            return await driver.execute_query(query, database_=database, **params)
        return await asyncio.to_thread(
            driver.execute_query, query, database_=database, **params
        )
    raise TypeError(f"Provided driver does not support execute_query: {type(driver)}")


class GDSProjectionManager:
    """Prepares an in-memory Neo4j GDS graph projection for community detection.

    Architectural relationships in the database remain directed. The GDS projection
    treats them as undirected ONLY within the in-memory projection to discover
    functional clusters.

    Uses GDS Cypher projection with explicit endpoint filtering to guarantee that
    only relationships between entity nodes are projected, preventing errors when
    heterogeneous graphs share relationship types between non-entity nodes.
    """

    def __init__(self, config: CommunityConfig | None = None) -> None:
        self.config = config or CommunityConfig()

    async def count_entities(
        self,
        driver: Any,
        database: str | None = None,
    ) -> int:
        """Return the total number of entity nodes in the database."""
        db = database or self.config.database
        label = self.config.entity_label
        query = f"MATCH (:{label}) RETURN count(*) AS cnt"
        try:
            result = await _execute_query(driver, query, database=db)
            records = getattr(result, "records", result)
            return int(records[0]["cnt"]) if records else 0
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to count entity nodes of label ':{label}': {exc}"
            ) from exc

    async def count_eligible_entities(
        self,
        driver: Any,
        database: str | None = None,
    ) -> int:
        """Count entities participating in at least one eligible entity-to-entity relationship."""
        db = database or self.config.database
        label = self.config.entity_label
        membership_rel = self.config.membership_relationship
        query = f"""
        MATCH (e:{label})
        WHERE EXISTS {{
            MATCH (e)-[r]-(neighbor:{label})
            WHERE type(r) <> $membership_rel
        }}
        RETURN count(e) AS cnt
        """
        try:
            result = await _execute_query(
                driver, query, database=db, membership_rel=membership_rel
            )
            records = getattr(result, "records", result)
            return int(records[0]["cnt"]) if records else 0
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to count community-eligible entity nodes: {exc}"
            ) from exc

    async def get_relationship_types(
        self,
        driver: Any,
        database: str | None = None,
    ) -> list[str]:
        """Discover all distinct relationship types connecting architectural entities.

        Strictly excludes membership relationships (e.g. MEMBER_OF) and community nodes.
        Requires both endpoints to carry the entity label.
        """
        db = database or self.config.database
        label = self.config.entity_label
        membership_rel = self.config.membership_relationship

        query = f"""
        MATCH (s:{label})-[r]->(t:{label})
        WHERE type(r) <> $membership_rel
        RETURN DISTINCT type(r) AS relationship_type
        ORDER BY relationship_type
        """
        try:
            result = await _execute_query(
                driver,
                query,
                database=db,
                membership_rel=membership_rel,
            )
            records = getattr(result, "records", result)
            rel_types: list[str] = []
            for record in records:
                rel_type = record["relationship_type"]
                if not _RELATIONSHIP_TYPE_PATTERN.match(rel_type):
                    logger.warning(
                        "Ignoring invalid relationship type '%s' for projection",
                        rel_type,
                    )
                    continue
                if rel_type == membership_rel:
                    continue
                rel_types.append(rel_type)

            return rel_types
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to discover architectural relationship types: {exc}"
            ) from exc

    async def projection_exists(
        self,
        driver: Any,
        graph_name: str | None = None,
        database: str | None = None,
    ) -> bool:
        """Check whether the named GDS projection already exists."""
        db = database or self.config.database
        gname = graph_name or self.config.graph_name

        query = """
        CALL gds.graph.exists($graph_name)
        YIELD exists
        RETURN exists
        """
        try:
            result = await _execute_query(driver, query, database=db, graph_name=gname)
            records = getattr(result, "records", result)
            if records:
                return bool(records[0]["exists"])
            return False
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to check GDS projection existence for '{gname}': {exc}"
            ) from exc

    async def drop_projection_if_exists(
        self,
        driver: Any,
        graph_name: str | None = None,
        database: str | None = None,
    ) -> bool:
        """Drop the GDS projection if it exists, freeing GDS memory."""
        db = database or self.config.database
        gname = graph_name or self.config.graph_name

        exists = await self.projection_exists(driver, graph_name=gname, database=db)
        if not exists:
            return False

        query = """
        CALL gds.graph.drop($graph_name)
        YIELD graphName
        RETURN graphName
        """
        try:
            await _execute_query(driver, query, database=db, graph_name=gname)
            logger.info("Dropped existing GDS projection: '%s'", gname)
            return True
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to drop GDS projection '{gname}': {exc}"
            ) from exc

    async def create_projection(
        self,
        driver: Any,
        graph_name: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Project the entity graph using GDS Cypher projection with undirected relationships.

        Guarantees:
        1. Every node carries the configured entity_label.
        2. Isolated entity nodes without any relationships are explicitly preserved.
        3. Only relationships where BOTH source and target have entity_label are projected.
        4. In-database relationships remain directed; they are loaded as UNDIRECTED in GDS memory.
        5. Membership relationships (e.g. MEMBER_OF) are strictly excluded.
        6. Empty entity graphs return clean zero statistics without error.
        """
        db = database or self.config.database
        gname = graph_name or self.config.graph_name
        label = self.config.entity_label
        membership_rel = self.config.membership_relationship

        # await self.validate_entity_ids(driver, database=db)
        entity_count = await self.count_entities(driver, database=db)
        eligible_entity_count = await self.count_eligible_entities(driver, database=db)
        if eligible_entity_count == 0:
            logger.info("No entity nodes found. Returning empty projection stats.")
            return {
                "graphName": gname,
                "nodeCount": 0,
                "relationshipCount": 0,
                "relationshipTypes": [],
                "eligibleNodeCount": 0,
                "totalEntityCount": entity_count,
            }

        # Discover relationship types for reporting/metadata
        rel_types = await self.get_relationship_types(driver, database=db)

        # Build GDS Cypher projection graph configuration
        graph_config: dict[str, Any] = {
            "undirectedRelationshipTypes": ["*"],
        }
        if self.config.gds_memory:
            graph_config["memory"] = self.config.gds_memory
        
        # if self.config.gds_ttl:
        #     graph_config["ttl"] = self.config.gds_ttl

        # Cypher projection query: only entities with at least one eligible
        # entity-to-entity relationship enter the derived community graph.
        query = f"""
        MATCH (source:{label})
        WHERE EXISTS {{
            MATCH (source)-[eligible_rel]-(eligible_target:{label})
            WHERE type(eligible_rel) <> $membership_rel
        }}
        OPTIONAL MATCH (source)-[r]->(target:{label})
        WHERE r IS NULL OR type(r) <> $membership_rel
        WITH gds.graph.project(
            $graph_name,
            source,
            target,
            {{
                sourceNodeLabels: labels(source),
                targetNodeLabels: CASE WHEN target IS NULL THEN [] ELSE labels(target) END,
                relationshipType: type(r)
            }},
            $graph_config
        ) AS g
        RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount
        """
        try:
            result = await _execute_query(
                driver,
                query,
                database=db,
                graph_name=gname,
                membership_rel=membership_rel,
                graph_config=graph_config,
            )
            records = getattr(result, "records", result)
            record = records[0]
            stats = {
                "graphName": record["graphName"],
                "nodeCount": record["nodeCount"],
                "relationshipCount": record["relationshipCount"],
                "relationshipTypes": rel_types,
                "eligibleNodeCount": eligible_entity_count,
                "totalEntityCount": entity_count,
            }

            if stats["nodeCount"] != eligible_entity_count:
                raise CommunityProjectionError(
                    f"GDS projection invariant failed: projected node count "
                    f"({stats['nodeCount']}) does not equal community-eligible "
                    f"entity count ({eligible_entity_count}). Total :{label} entities: "
                    f"{entity_count}."
                )

            logger.info(
                "Created GDS Cypher projection '%s': %d nodes, %d relationships (%d types)",
                gname,
                stats["nodeCount"],
                stats["relationshipCount"],
                len(rel_types),
            )
            return stats
        except Exception as exc:
            raise CommunityProjectionError(
                f"Failed to create GDS projection '{gname}': {exc}"
            ) from exc

    async def ensure_projection(
        self,
        driver: Any,
        graph_name: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Drop any existing projection with the target name, then create a fresh projection."""
        gname = graph_name or self.config.graph_name
        db = database or self.config.database
        await self.drop_projection_if_exists(driver, graph_name=gname, database=db)
        return await self.create_projection(driver, graph_name=gname, database=db)
