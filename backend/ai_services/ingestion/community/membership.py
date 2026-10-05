"""Community store and membership management for Neo4j."""

from __future__ import annotations

from collections import defaultdict
import logging
from typing import Any, Sequence

from .config import CommunityConfig
from .exceptions import CommunityPersistenceError
from .interfaces import CommunityStore
from .models import (
    Community,
    CommunityContext,
    CommunityEmbedding,
    CommunityMember,
    CommunityProcessingState,
    CommunitySummary,
    DirectedRelationship,
)
from .projection import _execute_query

logger = logging.getLogger(__name__)


class Neo4jCommunityStore(CommunityStore):
    """Neo4j implementation of CommunityStore.

    Handles idempotent creation of Community nodes, membership relationships,
    directed context extraction, and property persistence.
    """

    def __init__(self, driver: Any, config: CommunityConfig | None = None) -> None:
        self.driver = driver
        self.config = config or CommunityConfig()

    async def persist_memberships(
        self,
        assignments: dict[str | int, list[str]],
        is_full_sync: bool = True,
    ) -> int:
        """Persist community assignments as one authoritative Neo4j write transaction.

        For a full sync, every existing MEMBER_OF relationship and community assignment
        is replaced by the supplied detection result. For targeted sync, only the
        supplied communities are updated and unrelated communities are left untouched.
        Canonical entity IDs are required; Neo4j elementId() is never used as a fallback.
        """
        db = self.config.database
        c_label = self.config.community_label
        e_label = self.config.entity_label
        rel_type = self.config.membership_relationship

        # A full sync with zero communities is authoritative: it means the architectural
        # graph currently contains no entities/communities, so stale community state must
        # be removed rather than silently left behind.
        items = [
            {"community_id": cid, "entity_ids": list(members)}
            for cid, members in assignments.items()
        ]
        expected_entity_ids = [
            str(entity_id)
            for item in items
            for entity_id in item["entity_ids"]
        ]

        if len(expected_entity_ids) != len(set(expected_entity_ids)):
            raise CommunityPersistenceError(
                "Detection result is invalid: the same canonical entity ID appears "
                "in multiple community assignments."
            )

        # Validate every assigned canonical ID before changing graph state.
        if expected_entity_ids:
            validate_query = f"""
            UNWIND $entity_ids AS eid
            OPTIONAL MATCH (e:{e_label})
            WHERE elementId(e) = eid
            WITH eid, count(e) AS matches
            WITH collect(CASE WHEN matches = 0 THEN eid END) AS missing_ids,
                 collect(CASE WHEN matches > 1 THEN eid END) AS duplicate_ids
            RETURN
                [x IN missing_ids WHERE x IS NOT NULL] AS missing_ids,
                [x IN duplicate_ids WHERE x IS NOT NULL] AS duplicate_ids
            """
            try:
                validation_result = await _execute_query(
                    self.driver,
                    validate_query,
                    database=db,
                    entity_ids=expected_entity_ids,
                )
                records = getattr(validation_result, "records", validation_result)
                record = records[0] if records else None
                missing_ids = list(record["missing_ids"] or []) if record else []
                duplicate_ids = list(record["duplicate_ids"] or []) if record else []
                if missing_ids or duplicate_ids:
                    raise CommunityPersistenceError(
                        "Community membership validation failed before persistence: "
                        f"missing canonical IDs={missing_ids[:10]}, "
                        f"duplicate canonical IDs={duplicate_ids[:10]}."
                    )
            except CommunityPersistenceError:
                raise
            except Exception as exc:
                raise CommunityPersistenceError(
                    f"Failed to validate community membership assignments: {exc}"
                ) from exc

        community_ids = [item["community_id"] for item in items]

        # All mutations for a full synchronization occur in one Cypher statement,
        # hence one Neo4j transaction. This prevents partially persisted memberships
        # when the query fails.
        if is_full_sync:
            query = f"""
            CALL () {{
                UNWIND $items AS item
                MERGE (c:{c_label} {{communityId: item.community_id}})
                SET c.entityCount = size(item.entity_ids)
                RETURN count(c) AS created_communities
            }}
            CALL () {{
                MATCH (e:{e_label})
                OPTIONAL MATCH (e)-[old_rel:{rel_type}]->(:{c_label})
                DELETE old_rel
                REMOVE e.communityId
                RETURN count(DISTINCT e) AS cleared_entities
            }}
            CALL () {{
                UNWIND $items AS item
                MATCH (c:{c_label} {{communityId: item.community_id}})
                UNWIND item.entity_ids AS eid
                MATCH (e:{e_label})
                WHERE elementId(e) = eid
                SET e.communityId = item.community_id
                MERGE (e)-[:{rel_type}]->(c)
                RETURN count(DISTINCT e) AS persisted_entities
            }}
            CALL () {{
                MATCH (c:{c_label})
                WHERE NOT c.communityId IN $community_ids
                DETACH DELETE c
                RETURN count(c) AS deleted_communities
            }}
            RETURN $expected_membership_count AS persisted_entities
            """
        else:
            query = f"""
            UNWIND $items AS item
            MERGE (c:{c_label} {{communityId: item.community_id}})
            SET c.entityCount = size(item.entity_ids)
            WITH collect(c) AS current_communities

            CALL {{
                UNWIND $items AS item
                MATCH (c:{c_label} {{communityId: item.community_id}})
                UNWIND item.entity_ids AS eid
                MATCH (e:{e_label})
                WHERE elementId(e) = eid
                SET e.communityId = item.community_id
                OPTIONAL MATCH (e)-[old_rel:{rel_type}]->(old_c:{c_label})
                WHERE old_c.communityId <> item.community_id
                DELETE old_rel
                MERGE (e)-[:{rel_type}]->(c)
                RETURN count(DISTINCT e) AS persisted_entities
            }}

            RETURN coalesce(persisted_entities, 0) AS persisted_entities
            """

        try:
            result = await _execute_query(
                self.driver,
                query,
                database=db,
                items=items,
                community_ids=community_ids,
                expected_membership_count=len(expected_entity_ids),
            )
            records = getattr(result, "records", result)
            persisted_count = (
                int(records[0]["persisted_entities"]) if records else 0
            )

            expected_count = len(expected_entity_ids)
            if persisted_count != expected_count:
                raise CommunityPersistenceError(
                    f"Community membership persistence invariant failed: "
                    f"expected {expected_count} memberships, persisted {persisted_count}."
                )

            # Validate exactly one membership per entity after the transaction.
            duplicate_check_query = f"""
            MATCH (e:{e_label})
            WHERE elementId(e) IN $entity_ids
            OPTIONAL MATCH (e)-[:{rel_type}]->(c:{c_label})
            WITH e, count(c) AS community_count
            WHERE community_count <> 1
            RETURN count(e) AS invalid_entities
            """
            duplicate_result = await _execute_query(
                self.driver,
                duplicate_check_query,
                database=db,
                is_full_sync=is_full_sync,
                entity_ids=expected_entity_ids,
            )
            duplicate_records = getattr(duplicate_result, "records", duplicate_result)
            invalid_entities = (
                int(duplicate_records[0]["invalid_entities"])
                if duplicate_records else 0
            )
            if invalid_entities:
                raise CommunityPersistenceError(
                    f"Membership integrity failure: {invalid_entities} entities "
                    f"do not have exactly one :{rel_type} relationship."
                )

            logger.info(
                "Persisted memberships across %d communities (%d entities, is_full_sync=%s)",
                len(assignments),
                persisted_count,
                is_full_sync,
            )
            return persisted_count
        except CommunityPersistenceError:
            raise
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to persist community memberships: {exc}"
            ) from exc

    async def cleanup_temporary_detection_properties(self) -> None:
        """Remove any leftover temporary GDS properties (e.g. _gdsCommunityId) from entity nodes."""
        db = self.config.database
        e_label = self.config.entity_label
        temp_prop = self.config.temporary_gds_property

        query = f"""
        MATCH (e:{e_label})
        WHERE e.{temp_prop} IS NOT NULL
        REMOVE e.{temp_prop}
        """
        try:
            await _execute_query(self.driver, query, database=db)
        except Exception as exc:
            logger.warning("Failed to clean up temporary GDS property '%s': %s", temp_prop, exc)

    async def get_processing_states(
        self,
        community_ids: Sequence[str | int],
    ) -> dict[str | int, CommunityProcessingState]:
        """Fetch persisted state of summaries and embeddings for change detection."""
        if not community_ids:
            return {}

        db = self.config.database
        c_label = self.config.community_label
        emb_prop = self.config.embedding_property

        query = f"""
        MATCH (c:{c_label})
        WHERE c.communityId IN $community_ids
        RETURN
            c.communityId AS community_id,
            (c.summary IS NOT NULL) AS has_summary,
            c.summary AS summary,
            c.architecturalRole AS architectural_role,
            coalesce(c.keyEntities, []) AS key_entities,
            c.summaryHash AS summary_hash,
            c.contextHash AS context_hash,
            (c.{emb_prop} IS NOT NULL) AS has_embedding,
            c.embeddedSummaryHash AS embedded_summary_hash
        """
        try:
            result = await _execute_query(
                self.driver, query, database=db, community_ids=list(community_ids)
            )
            records = getattr(result, "records", result)
            states: dict[str | int, CommunityProcessingState] = {}
            for rec in records:
                cid = rec["community_id"]
                states[cid] = CommunityProcessingState(
                    community_id=cid,
                    has_summary=bool(rec["has_summary"]),
                    summary=rec["summary"],
                    architectural_role=rec["architectural_role"],
                    key_entities=list(rec["key_entities"] or []),
                    summary_hash=rec["summary_hash"],
                    context_hash=rec["context_hash"],
                    has_embedding=bool(rec["has_embedding"]),
                    embedded_summary_hash=rec["embedded_summary_hash"],
                )
            return states
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to fetch community processing states: {exc}"
            ) from exc

    async def load_community_contexts(
        self,
        community_ids: Sequence[str | int] | None = None,
    ) -> list[CommunityContext]:
        """Load internal architectural contexts for communities, strictly preserving edge direction.

        External relationships (where one endpoint is outside the community) are excluded.
        """
        db = self.config.database
        c_label = self.config.community_label
        e_label = self.config.entity_label
        rel_type = self.config.membership_relationship

        c_ids_param = list(community_ids) if community_ids is not None else None

        query = f"""
        MATCH (e:{e_label})-[:{rel_type}]->(c:{c_label})
        WHERE ($community_ids IS NULL OR c.communityId IN $community_ids)
        OPTIONAL MATCH (e)-[r]->(neighbor:{e_label})-[:{rel_type}]->(c)
        WHERE e <> neighbor AND type(r) <> $rel_type
        RETURN
            c.communityId AS community_id,
            toString(elementId(e)) AS entity_id,
            coalesce(e.name, toString(elementId(e))) AS entity_name,
            labels(e) AS entity_labels,
            type(r) AS relationship_type,
            toString(elementId(neighbor)) AS target_id,
            coalesce(neighbor.name, toString(elementId(neighbor))) AS target_name
        ORDER BY c.communityId, entity_name, target_name
        """
        try:
            result = await _execute_query(
                self.driver,
                query,
                database=db,
                community_ids=c_ids_param,
                rel_type=rel_type,
            )
            records = getattr(result, "records", result)

            members_by_comm: dict[str | int, dict[str, CommunityMember]] = defaultdict(dict)
            rels_by_comm: dict[str | int, set[tuple[str, str, str, str, str]]] = defaultdict(set)

            for rec in records:
                cid = rec["community_id"]
                eid = str(rec["entity_id"])
                ename = rec["entity_name"] or eid
                elabels = list(rec["entity_labels"] or [])

                if eid not in members_by_comm[cid]:
                    members_by_comm[cid][eid] = CommunityMember(
                        entity_id=eid,
                        entity_name=ename,
                        labels=elabels,
                    )

                rel_t = rec["relationship_type"]
                if rel_t is not None:
                    target_id = str(rec["target_id"])
                    target_name = rec["target_name"] or target_id
                    rel_tuple = (eid, ename, rel_t, target_id, target_name)
                    rels_by_comm[cid].add(rel_tuple)

            contexts: list[CommunityContext] = []
            for cid, members_dict in members_by_comm.items():
                rels = [
                    DirectedRelationship(
                        source_id=t[0],
                        source_name=t[1],
                        relationship_type=t[2],
                        target_id=t[3],
                        target_name=t[4],
                    )
                    for t in rels_by_comm[cid]
                ]
                contexts.append(
                    CommunityContext(
                        community_id=cid,
                        members=list(members_dict.values()),
                        relationships=rels,
                    )
                )

            return contexts
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to load community contexts: {exc}"
            ) from exc

    async def persist_summaries(
        self,
        summaries: Sequence[CommunitySummary],
    ) -> None:
        """Persist structured community summaries to Neo4j.

        All persisted properties are Neo4j scalar values or list of scalars.
        """
        if not summaries:
            return

        db = self.config.database
        c_label = self.config.community_label

        items = [
            {
                "community_id": s.community_id,
                "summary": s.summary,
                "architectural_role": s.architectural_role,
                "key_entities": list(s.key_entities),
                "summary_hash": s.summary_hash,
                "context_hash": s.context_hash,
            }
            for s in summaries
        ]

        query = f"""
        UNWIND $items AS item
        MERGE (c:{c_label} {{communityId: item.community_id}})
        SET c.summary = item.summary,
            c.architecturalRole = item.architectural_role,
            c.keyEntities = item.key_entities,
            c.summaryHash = item.summary_hash,
            c.contextHash = item.context_hash
        """
        try:
            await _execute_query(self.driver, query, database=db, items=items)
            logger.info("Persisted summaries for %d communities", len(summaries))
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to persist community summaries: {exc}"
            ) from exc

    async def persist_embeddings(
        self,
        embeddings: Sequence[CommunityEmbedding],
        embedding_version_hash: str | None = None,
    ) -> None:
        """Persist vector embeddings and embedded version hash to Community nodes."""
        if not embeddings:
            return

        db = self.config.database
        c_label = self.config.community_label
        emb_prop = self.config.embedding_property

        items = [
            {
                "community_id": e.community_id,
                "embedding": e.embedding,
                "summary_hash": embedding_version_hash or e.summary_hash,
            }
            for e in embeddings
        ]

        query = f"""
        UNWIND $items AS item
        MATCH (c:{c_label} {{communityId: item.community_id}})
        SET c.{emb_prop} = item.embedding,
            c.embeddedSummaryHash = item.summary_hash
        """
        try:
            await _execute_query(self.driver, query, database=db, items=items)
            logger.info("Persisted embeddings for %d communities", len(embeddings))
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to persist community embeddings: {exc}"
            ) from exc

    async def get_communities(
        self,
        community_ids: Sequence[str | int] | None = None,
    ) -> list[Community]:
        """Fetch Community domain entities from Neo4j."""
        db = self.config.database
        c_label = self.config.community_label
        emb_prop = self.config.embedding_property
        c_ids_param = list(community_ids) if community_ids is not None else None

        query = f"""
        MATCH (c:{c_label})
        WHERE ($community_ids IS NULL OR c.communityId IN $community_ids)
        RETURN
            c.communityId AS community_id,
            coalesce(c.entityCount, 0) AS entity_count,
            c.summary AS summary,
            c.architecturalRole AS architectural_role,
            coalesce(c.keyEntities, []) AS key_entities,
            c.summaryHash AS summary_hash,
            c.contextHash AS context_hash,
            c.{emb_prop} AS embedding
        ORDER BY c.communityId
        """
        try:
            result = await _execute_query(
                self.driver, query, database=db, community_ids=c_ids_param
            )
            records = getattr(result, "records", result)
            return [
                Community(
                    community_id=rec["community_id"],
                    entity_count=int(rec["entity_count"]),
                    summary=rec["summary"],
                    architectural_role=rec["architectural_role"],
                    key_entities=list(rec["key_entities"] or []),
                    summary_hash=rec["summary_hash"],
                    context_hash=rec["context_hash"],
                    embedding=rec["embedding"],
                )
                for rec in records
            ]
        except Exception as exc:
            raise CommunityPersistenceError(
                f"Failed to retrieve Community records: {exc}"
            ) from exc
