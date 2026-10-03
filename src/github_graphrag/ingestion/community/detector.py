"""Community detection implementations using Neo4j Graph Data Science (GDS)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Sequence

from .config import CommunityConfig
from .exceptions import CommunityDetectionError
from .interfaces import CommunityDetector, CommunityIdentityStrategy
from .models import CommunityDetectionResult
from .projection import _execute_query

logger = logging.getLogger(__name__)


class SnapshotCommunityIdStrategy(CommunityIdentityStrategy):
    """Generates community IDs directly from the GDS partition ID for the current snapshot.

    SEMANTICS & LIMITATIONS:
    - This identity is valid ONLY within the current detected graph snapshot.
    - GDS integer partition IDs are NOT guaranteed to be globally stable across independent runs,
      re-projections, or repository snapshots.
    """

    def generate_id(
        self,
        raw_community_id: int | str,
        member_ids: Sequence[str],
    ) -> str | int:
        return raw_community_id


class DeterministicCommunityIdStrategy(CommunityIdentityStrategy):
    """Generates deterministic community IDs derived from the sorted canonical entity IDs.

    SEMANTICS & LIMITATIONS:
    - Derives an ID via SHA-256 over the lexicographically sorted canonical entity IDs.
    - Stable ONLY when the exact member entity set remains identical.
    - If a community's membership changes (entity added, moved, or deleted), the resulting
      derived ID WILL change. It does not provide cross-snapshot lineage for evolving clusters.
    """

    def generate_id(
        self,
        raw_community_id: int | str,
        member_ids: Sequence[str],
    ) -> str:
        sorted_members = sorted(str(m) for m in member_ids)
        content = "|".join(sorted_members)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        return f"comm_{digest}"


class GDSLeidenDetector(CommunityDetector):
    """Executes the Leiden community detection algorithm via Neo4j Graph Data Science."""

    def __init__(
        self,
        config: CommunityConfig | None = None,
        identity_strategy: CommunityIdentityStrategy | None = None,
    ) -> None:
        self.config = config or CommunityConfig()
        self.identity_strategy = identity_strategy or DeterministicCommunityIdStrategy()

    async def detect(
        self,
        driver: Any,
        graph_name: str,
        database: str | None = None,
    ) -> CommunityDetectionResult:
        """Run Leiden community detection on the projected graph and return final domain assignments."""
        db = database or self.config.database
        temp_prop = self.config.temporary_gds_property
        label = self.config.entity_label

        logger.info(
            "Running Leiden community detection on projection '%s' with randomSeed=%d, writing to temporary property '%s'",
            graph_name,
            self.config.random_seed,
            temp_prop,
        )

        query = """
        CALL gds.leiden.write(
            $graph_name,
            {
                writeProperty: $write_property,
                randomSeed: $random_seed
            }
        )
        YIELD communityCount, modularity, nodeCount
        RETURN communityCount, modularity, nodeCount
        """
        try:
            result = await _execute_query(
                driver,
                query,
                database=db,
                graph_name=graph_name,
                write_property=temp_prop,
                random_seed=self.config.random_seed,
            )
            records = getattr(result, "records", result)
            if not records:
                raise CommunityDetectionError(
                    f"GDS Leiden returned no execution results on graph '{graph_name}'"
                )

            stats = records[0]
            comm_count = int(stats["communityCount"])
            node_count = int(stats["nodeCount"])
            logger.info(
                "GDS Leiden detected %d raw partitions across %d entities",
                comm_count,
                node_count,
            )

            # Retrieve raw assignments using canonical entity ID property
            assignments_query = f"""
            MATCH (e:{label})
            WHERE e[$write_property] IS NOT NULL
            RETURN e[$write_property] AS raw_community_id,
                collect(toString(elementId(e))) AS entity_ids
            ORDER BY raw_community_id
            """
            assign_result = await _execute_query(
                driver,
                assignments_query,
                database=db,
                write_property=temp_prop,
            )
            assign_records = getattr(assign_result, "records", assign_result)

            raw_groups: list[tuple[Any, list[str]]] = []
            for rec in assign_records:
                raw_id = rec["raw_community_id"]
                members = [str(eid) for eid in rec["entity_ids"] if eid is not None]
                raw_groups.append((raw_id, members))

            if any(not members for _, members in raw_groups):
                raise CommunityDetectionError(
                    "Leiden produced an empty community partition, which is invalid."
                )
            assigned_ids = [member_id for _, members in raw_groups for member_id in members]
            if len(assigned_ids) != node_count:
                raise CommunityDetectionError(
                    f"Leiden assignment invariant failed: GDS reported {node_count} nodes, "
                    f"but {len(assigned_ids)} canonical entity assignments were read back."
                )
            if len(set(assigned_ids)) != len(assigned_ids):
                raise CommunityDetectionError(
                    "Leiden assignment invariant failed: at least one canonical entity ID "
                    "appears in more than one detected community."
                )

            # Transform raw IDs through identity strategy
            if any(not members for _, members in raw_groups):
                raise CommunityDetectionError(
                    "Louvain produced an empty community partition, which is invalid."
                )
            assigned_ids = [member_id for _, members in raw_groups for member_id in members]
            if len(assigned_ids) != node_count:
                raise CommunityDetectionError(
                    f"Louvain assignment invariant failed: GDS reported {node_count} nodes, "
                    f"but {len(assigned_ids)} canonical entity assignments were read back."
                )
            if len(set(assigned_ids)) != len(assigned_ids):
                raise CommunityDetectionError(
                    "Louvain assignment invariant failed: at least one canonical entity ID "
                    "appears in more than one detected community."
                )

            final_assignments: dict[str | int, list[str]] = {}
            for raw_id, members in raw_groups:
                final_id = self.identity_strategy.generate_id(raw_id, members)
                if final_id in final_assignments:
                    raise CommunityDetectionError(
                        f"Community identity collision: Two distinct raw partitions mapped to the identical "
                        f"final community ID '{final_id}' using {type(self.identity_strategy).__name__}. "
                        "Cannot safely proceed with duplicate community identities."
                    )
                final_assignments[final_id] = members

            return CommunityDetectionResult(
                algorithm="leiden",
                community_count=len(final_assignments),
                node_count=node_count,
                assignments=final_assignments,
            )
        except Exception as exc:
            if isinstance(exc, CommunityDetectionError):
                raise
            raise CommunityDetectionError(
                f"Leiden detection failed on graph '{graph_name}': {exc}"
            ) from exc


class GDSLouvainDetector(CommunityDetector):
    """Executes the Louvain community detection algorithm via Neo4j Graph Data Science."""

    def __init__(
        self,
        config: CommunityConfig | None = None,
        identity_strategy: CommunityIdentityStrategy | None = None,
    ) -> None:
        self.config = config or CommunityConfig()
        self.identity_strategy = identity_strategy or DeterministicCommunityIdStrategy()

    async def detect(
        self,
        driver: Any,
        graph_name: str,
        database: str | None = None,
    ) -> CommunityDetectionResult:
        """Run Louvain community detection on the projected graph and return final domain assignments."""
        db = database or self.config.database
        temp_prop = self.config.temporary_gds_property
        label = self.config.entity_label

        logger.info(
            "Running Louvain community detection on projection '%s' with randomSeed=%d, writing to temporary property '%s'",
            graph_name,
            self.config.random_seed,
            temp_prop,
        )

        query = """
        CALL gds.louvain.write(
            $graph_name,
            {
                writeProperty: $write_property,
                randomSeed: $random_seed
            }
        )
        YIELD communityCount, modularity, nodeCount
        RETURN communityCount, modularity, nodeCount
        """
        try:
            result = await _execute_query(
                driver,
                query,
                database=db,
                graph_name=graph_name,
                write_property=temp_prop,
                random_seed=self.config.random_seed,
            )
            records = getattr(result, "records", result)
            if not records:
                raise CommunityDetectionError(
                    f"GDS Louvain returned no execution results on graph '{graph_name}'"
                )

            stats = records[0]
            comm_count = int(stats["communityCount"])
            node_count = int(stats["nodeCount"])
            logger.info(
                "GDS Louvain detected %d raw partitions across %d entities",
                comm_count,
                node_count,
            )

            assignments_query = f"""
            MATCH (e:{label})
            WHERE e[$write_property] IS NOT NULL
            RETURN e[$write_property] AS raw_community_id,
                   collect(toString(elementId(e))) AS entity_ids
            ORDER BY raw_community_id
            """
            assign_result = await _execute_query(
                driver,
                assignments_query,
                database=db,
                write_property=temp_prop,
            )
            assign_records = getattr(assign_result, "records", assign_result)

            raw_groups: list[tuple[Any, list[str]]] = []
            for rec in assign_records:
                raw_id = rec["raw_community_id"]
                members = [str(eid) for eid in rec["entity_ids"] if eid is not None]
                raw_groups.append((raw_id, members))

            final_assignments: dict[str | int, list[str]] = {}
            for raw_id, members in raw_groups:
                final_id = self.identity_strategy.generate_id(raw_id, members)
                if final_id in final_assignments:
                    raise CommunityDetectionError(
                        f"Community identity collision: Two distinct raw partitions mapped to the identical "
                        f"final community ID '{final_id}' using {type(self.identity_strategy).__name__}."
                    )
                final_assignments[final_id] = members

            return CommunityDetectionResult(
                algorithm="louvain",
                community_count=len(final_assignments),
                node_count=node_count,
                assignments=final_assignments,
            )
        except Exception as exc:
            if isinstance(exc, CommunityDetectionError):
                raise
            raise CommunityDetectionError(
                f"Louvain detection failed on graph '{graph_name}': {exc}"
            ) from exc
