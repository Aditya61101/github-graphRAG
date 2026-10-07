from __future__ import annotations

import logging
from typing import Any

from ai_services.graph.identity import clean_entity_id, edge_graph_id, entity_graph_id
from ai_services.graph.repository import GraphRepositoryError, Neo4jGraphRepository
from ai_services.ingestion.persistence.models import RepositoryModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from api_services.app.models.graph import GraphEdge, GraphNode, RepositoryGraphResponse

logger = logging.getLogger(__name__)


class RepositoryNotFoundError(Exception):
    """Raised when repository does not exist in the application store."""
    pass


class RepositoryAccessDeniedError(Exception):
    """Raised when the authenticated user does not have permission to access the repository."""
    pass


class RepositoryGraphService:
    """Coordinates repository authorization and graph retrieval for frontend interactive visualization."""

    def __init__(
        self,
        sqlite_store: SqliteApplicationStore,
        graph_repo: Neo4jGraphRepository,
    ) -> None:
        self.sqlite_store = sqlite_store
        self.graph_repo = graph_repo

    def validate_repository_access(self, repository_identifier: str, user_id: str) -> RepositoryModel:
        """Verify repository exists and belongs to the authenticated user.

        Raises:
            RepositoryNotFoundError: if repository is not found (404)
            RepositoryAccessDeniedError: if repository belongs to another user (403)
        """
        repo = self.sqlite_store.get_repository(repository_identifier)
        if not repo:
            logger.warning(f"Graph fetch rejected: repository '{repository_identifier}' not found.")
            raise RepositoryNotFoundError(f"Repository '{repository_identifier}' was not found.")

        is_owner = (repo.user_id == user_id)
        has_connection = bool(
            repo.github_connection and repo.github_connection.user_id == user_id
        )
        if not (is_owner or has_connection):
            logger.warning(
                f"Graph fetch forbidden: user '{user_id}' denied access to repository '{repository_identifier}'."
            )
            raise RepositoryAccessDeniedError(
                f"Access denied: You do not have permission to access repository '{repository_identifier}'."
            )

        return repo

    async def get_repository_graph(
        self,
        repository_identifier: str,
        user_id: str,
    ) -> RepositoryGraphResponse:
        """Retrieve the canonical entity-relationship architectural graph for the specified repository."""
        # 1. Authorize repository access
        repo = self.validate_repository_access(repository_identifier, user_id)
        canonical_repo_id = repo.id
        repo_ids = list(dict.fromkeys(filter(None, [repo.id, repo.github_repository_id])))

        # 2. Fetch canonical architectural entities from Neo4j
        raw_entities = self.graph_repo.fetch_entities(repo_ids)

        nodes: list[GraphNode] = []
        seen_node_ids: set[str] = set()
        valid_node_ids: set[str] = set()

        for ent in raw_entities:
            raw_id = ent.get("id")
            node_id = entity_graph_id(raw_id)
            if not node_id:
                continue

            clean_id = clean_entity_id(raw_id)

            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            valid_node_ids.add(node_id)

            label = str(ent.get("name") or "").strip() or clean_id
            type_ = str(ent.get("label") or "").strip() or "Entity"

            metadata: dict[str, Any] = {
                "repository_id": canonical_repo_id,
            }
            if ent.get("aliases"):
                metadata["aliases"] = ent["aliases"]
            if ent.get("description"):
                metadata["description"] = ent["description"]
            if ent.get("source"):
                metadata["source"] = ent["source"]

            nodes.append(
                GraphNode(
                    id=node_id,
                    label=label,
                    type=type_,
                    metadata=metadata,
                )
            )

        # 3. If there are no entities for this repository, return an empty graph immediately
        if not nodes:
            return RepositoryGraphResponse(nodes=[], edges=[])

        # 4. Fetch direct code relationships and assertion-backed (e.g. ADR) relationships
        direct_rels = self.graph_repo.fetch_direct_relationships(repo_ids)
        assertion_rels = self.graph_repo.fetch_assertion_relationships(repo_ids)

        all_raw_edges: list[tuple[dict[str, Any], str]] = []
        for r in direct_rels:
            all_raw_edges.append((r, str(r.get("source_type") or "code")))
        for r in assertion_rels:
            all_raw_edges.append((r, str(r.get("source_type") or "ADR")))

        # 5. Transform and deduplicate relationships into canonical architectural edges
        edges_map: dict[str, GraphEdge] = {}

        for rel, source_kind in all_raw_edges:
            src_raw = rel.get("source_id")
            tgt_raw = rel.get("target_id")
            rel_type = rel.get("rel_type")

            src_node_id = entity_graph_id(src_raw)
            tgt_node_id = entity_graph_id(tgt_raw)
            edge_id = edge_graph_id(src_raw, rel_type, tgt_raw)

            if not src_node_id or not tgt_node_id or not edge_id:
                continue

            # Only return relationships whose source and target entities belong to the requested repository
            if src_node_id not in valid_node_ids or tgt_node_id not in valid_node_ids:
                continue
            confidence = rel.get("confidence")
            rationale = rel.get("rationale")

            if edge_id not in edges_map:
                edge_meta: dict[str, Any] = {}
                if confidence is not None:
                    edge_meta["confidence"] = confidence
                if rationale:
                    edge_meta["rationale"] = rationale
                edge_meta["sources"] = [source_kind]

                edges_map[edge_id] = GraphEdge(
                    id=edge_id,
                    source=src_node_id,
                    target=tgt_node_id,
                    type=rel_type,
                    metadata=edge_meta,
                )
            else:
                existing = edges_map[edge_id]
                sources = existing.metadata.setdefault("sources", [])
                if source_kind not in sources:
                    sources.append(source_kind)
                if confidence is not None and (
                    "confidence" not in existing.metadata or confidence > existing.metadata["confidence"]
                ):
                    existing.metadata["confidence"] = confidence
                if rationale and not existing.metadata.get("rationale"):
                    existing.metadata["rationale"] = rationale

        # 6. Sort nodes and edges for deterministic presentation
        sorted_nodes = sorted(nodes, key=lambda n: n.id)
        sorted_edges = sorted(edges_map.values(), key=lambda e: e.id)

        return RepositoryGraphResponse(nodes=sorted_nodes, edges=sorted_edges)
