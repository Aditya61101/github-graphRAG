"""Vector index manager for Neo4j community embeddings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import CommunityConfig
from .exceptions import CommunityVectorIndexError
from .interfaces import VectorIndexManager
from .projection import _execute_query

logger = logging.getLogger(__name__)


class Neo4jVectorIndexManager(VectorIndexManager):
    """Manages the creation, verification, dimension validation, and readiness of the community vector index."""

    def __init__(self, driver: Any, config: CommunityConfig | None = None) -> None:
        self.driver = driver
        self.config = config or CommunityConfig()

    async def get_index_details(
        self,
        index_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Query Neo4j for existing index metadata by name."""
        name = index_name or self.config.vector_index_name
        db = self.config.database

        query = """
        SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state, options
        WHERE name = $name
        """
        try:
            result = await _execute_query(self.driver, query, database=db, name=name)
            records = getattr(result, "records", result)
            if records:
                rec = records[0]
                return {
                    "name": rec["name"],
                    "type": rec["type"],
                    "labels": rec.get("labelsOrTypes") or [],
                    "properties": rec.get("properties") or [],
                    "state": rec.get("state"),
                    "options": rec.get("options") or {},
                }
            return None
        except Exception as exc:
            raise CommunityVectorIndexError(
                f"Failed to inspect existing vector index '{name}': {exc}"
            ) from exc

    async def ensure_index(self, expected_dimensions: int) -> None:
        """Validate an existing vector index or create a new one matching the expected dimensions."""
        if expected_dimensions <= 0:
            raise CommunityVectorIndexError(
                f"Expected dimensions must be positive, got {expected_dimensions}"
            )

        name = self.config.vector_index_name
        db = self.config.database
        c_label = self.config.community_label
        emb_prop = self.config.embedding_property
        sim_fn = self.config.similarity_function

        existing = await self.get_index_details(name)

        if existing is not None:
            # 1. Validate index type
            idx_type = str(existing.get("type", "")).upper()
            if idx_type != "VECTOR":
                raise CommunityVectorIndexError(
                    f"Index '{name}' already exists with type '{idx_type}', expected 'VECTOR'."
                )

            # 2. Validate indexed label
            existing_labels = existing.get("labels") or []
            if c_label not in existing_labels:
                raise CommunityVectorIndexError(
                    f"Vector index '{name}' indexes labels {existing_labels}, expected ':{c_label}'."
                )

            # 3. Validate indexed property
            existing_props = existing.get("properties") or []
            if emb_prop not in existing_props:
                raise CommunityVectorIndexError(
                    f"Vector index '{name}' indexes properties {existing_props}, expected property '{emb_prop}'."
                )

            # 4. Validate dimensions and similarity function from options
            options = existing.get("options", {})
            cfg = options.get("indexConfig", {}) if isinstance(options, dict) else {}
            existing_dims = cfg.get("vector.dimensions") or options.get("vector.dimensions")
            existing_sim = cfg.get("vector.similarity_function") or options.get("vector.similarity_function")

            if existing_dims is not None and int(existing_dims) != expected_dimensions:
                raise CommunityVectorIndexError(
                    f"Vector index '{name}' exists with {existing_dims} dimensions, "
                    f"but the configured embedder produces {expected_dimensions} dimensions. "
                    "Cannot silently use incompatible dimensions. Please drop or rename the index."
                )

            if existing_sim is not None and str(existing_sim).lower() != sim_fn.lower():
                raise CommunityVectorIndexError(
                    f"Vector index '{name}' exists with similarity function '{existing_sim}', "
                    f"but configuration specifies '{sim_fn}'."
                )

            logger.info(
                "Verified existing community vector index '%s' (dimensions=%d, label=:%s, property=%s)",
                name,
                expected_dimensions,
                c_label,
                emb_prop,
            )
            return

        # Index does not exist: create it
        create_query = f"""
        CREATE VECTOR INDEX `{name}` IF NOT EXISTS
        FOR (n:{c_label}) ON n.{emb_prop}
        OPTIONS {{
            indexConfig: {{
                `vector.dimensions`: toInteger($dimensions),
                `vector.similarity_function`: $similarity_fn
            }}
        }}
        """
        try:
            await _execute_query(
                self.driver,
                create_query,
                database=db,
                dimensions=expected_dimensions,
                similarity_fn=sim_fn,
            )
            logger.info(
                "Created Neo4j vector index '%s' for :%s.%s (dims=%d, metric=%s)",
                name,
                c_label,
                emb_prop,
                expected_dimensions,
                sim_fn,
            )
        except Exception as exc:
            raise CommunityVectorIndexError(
                f"Failed to create community vector index '{name}': {exc}"
            ) from exc

    async def await_online(
        self,
        index_name: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Wait until the vector index reaches the ONLINE state.

        Args:
            index_name: Index name to check (defaults to configured index).
            timeout_seconds: Max seconds to wait before raising CommunityVectorIndexError.
            poll_interval: Polling frequency in seconds.
        """
        name = index_name or self.config.vector_index_name
        if timeout_seconds <= 0:
            return True

        elapsed = 0.0
        while elapsed < timeout_seconds:
            details = await self.get_index_details(name)
            if details is not None:
                state = str(details.get("state", "")).upper()
                if state == "ONLINE":
                    logger.info("Vector index '%s' is ONLINE.", name)
                    return True
                if state == "FAILED":
                    raise CommunityVectorIndexError(
                        f"Vector index '{name}' is in FAILED state in Neo4j."
                    )
                logger.debug("Awaiting vector index '%s' (current state: %s)...", name, state)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise CommunityVectorIndexError(
            f"Timed out after {timeout_seconds}s waiting for vector index '{name}' to become ONLINE."
        )
