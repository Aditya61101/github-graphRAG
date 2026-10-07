from __future__ import annotations

import logging
from typing import Any, Sequence

from ai_services.embeddings.base import Embedder
from ai_services.ingestion.rkg.canonicalization import (
    EntityCanonicalizer,
    normalized_entity_key,
)
from ai_services.ingestion.rkg.models import CanonicalEntity, ExtractedEntity

logger = logging.getLogger(__name__)


import re

def _strip_name(s: str) -> str:
    """Normalize name by removing whitespace, hyphens, and underscores for cross-casing/spacing matching."""
    return re.sub(r"[\s_\-]+", "", s).casefold()


def are_labels_compatible(label1: str | None, label2: str | None) -> bool:
    """Check if two entity labels are structurally compatible.

    Generic labels ('Component', 'Entity', 'Module') are compatible with specific
    subtypes. However, distinct specific architectural types (e.g. 'Database' vs 'Service')
    must not match to prevent cross-type collisions.
    """
    if not label1 or not label2:
        return True
    l1 = label1.strip().casefold()
    l2 = label2.strip().casefold()
    if l1 == l2:
        return True
    generics = {"component", "entity", "module", "architecturecomponent"}
    return l1 in generics or l2 in generics


class ADREntityResolver:
    """Resolves ADR entity references against the existing code knowledge graph.

    Ensures that entities already discovered from source code (e.g. PaymentService)
    are reused rather than duplicated. ADR-specific entities that do not exist
    in code are created with explicit provenance (source="ADR", source_id=adr_id).
    """

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        embedder: Embedder | None = None,
        embedding_dimensions: int = 3072,
    ) -> None:
        self.driver = driver
        self.database = database
        if embedder is None:
            raise ValueError("ADREntityResolver requires an embedder for Phase 2")
        self.embedder = embedder
        self.embedding_dimensions = embedding_dimensions

    def _load_existing_repo_entities(self, repository_id: str) -> dict[str, dict[str, Any]]:
        """Load all existing Entity nodes for the given repository from Neo4j."""
        existing: dict[str, dict[str, Any]] = {}
        if not self.driver:
            return existing

        try:
            records, _, _ = self.driver.execute_query(
                """
                MATCH (e:Entity {repository: $repo})
                RETURN e.id AS id, e.name AS name, e.label AS label,
                       coalesce(e.aliases, []) AS aliases, e.embedding AS embedding
                """,
                repo=repository_id,
                database_=self.database,
            )
            for r in records:
                row = dict(r) if hasattr(r, "keys") else r
                eid = row.get("id")
                if eid:
                    existing[eid] = {
                        "id": eid,
                        "name": row.get("name"),
                        "label": row.get("label"),
                        "aliases": list(row.get("aliases") or []),
                        "embedding": row.get("embedding"),
                    }
        except Exception as exc:
            logger.exception(f"Failed to load existing repository entities from Neo4j: {exc}")
            raise RuntimeError(
                f"Failed to load existing repository entities from Neo4j: {exc}"
            ) from exc

        return existing

    async def resolve_entities(
        self,
        repository_id: str,
        adr_id: str,
        raw_entities: Sequence[tuple[str, str, str]],  # (name, label, chunk_id)
    ) -> tuple[dict[str, CanonicalEntity], dict[str, str]]:
        """Resolve entity references to canonical IDs.

        Returns:
            (canonical_entities, name_to_canonical_id)
            - canonical_entities: map of cid -> CanonicalEntity (new or updated)
            - name_to_canonical_id: map of normalized name -> canonical_id
        """
        existing_nodes = self._load_existing_repo_entities(repository_id)

        # Build all canonicalization state per repository/ingestion. Never retain aliases
        # on the application-scoped resolver, otherwise entities from one repository can
        # resolve into another repository.
        canonicalizer = EntityCanonicalizer()
        name_to_ids: dict[str, set[str]] = {}
        stripped_to_ids: dict[str, set[str]] = {}
        key_to_id: dict[tuple[str, str], str] = {}

        for eid, node in existing_nodes.items():
            n_name = (node.get("name") or "").strip()
            n_label = (node.get("label") or "").strip()
            cf_name = n_name.casefold()
            s_name = _strip_name(n_name)

            if cf_name:
                name_to_ids.setdefault(cf_name, set()).add(eid)
            if s_name:
                stripped_to_ids.setdefault(s_name, set()).add(eid)
            if cf_name and n_label:
                key_to_id[(n_label.casefold(), cf_name)] = eid
                canonicalizer.alias_registry.add(n_label, n_name, eid)

            for alias in node.get("aliases") or []:
                a_name = str(alias).strip()
                cf_alias = a_name.casefold()
                sa_name = _strip_name(a_name)
                if cf_alias:
                    name_to_ids.setdefault(cf_alias, set()).add(eid)
                if sa_name:
                    stripped_to_ids.setdefault(sa_name, set()).add(eid)
                if n_label and cf_alias:
                    key_to_id[(n_label.casefold(), cf_alias)] = eid
                    canonicalizer.alias_registry.add(n_label, a_name, eid)

        resolved_canonical: dict[str, CanonicalEntity] = {}
        name_lookup_mapping: dict[str, str] = {}

        def add_lookup(label: str, name: str, canonical_id: str) -> None:
            clean = name.strip().casefold()
            if not clean:
                return
            key = f"{label.strip().casefold()}::{clean}"
            name_lookup_mapping[key] = canonical_id
            # Keep a bare-name mapping only when the name is unambiguous.
            existing = name_lookup_mapping.get(clean)
            if existing is None:
                name_lookup_mapping[clean] = canonical_id
            elif existing != canonical_id:
                name_lookup_mapping.pop(clean, None)

        new_entities_to_embed: list[CanonicalEntity] = []

        for name, label, chunk_id in raw_entities:
            clean_name = name.strip()
            clean_label = (label.strip() if label else "Component")
            if not clean_name:
                continue

            n_key = normalized_entity_key(clean_label, clean_name)
            cf_name = clean_name.casefold()
            s_name = _strip_name(clean_name)

            # Priority 1: Exact label + name match against existing graph
            matched_cid = key_to_id.get(n_key) if n_key else None

            # Priority 2: AliasRegistry match
            if not matched_cid:
                matched_cid = canonicalizer.alias_registry.resolve(clean_label, clean_name)

            # Priority 3: Case-insensitive name match with label compatibility check
            if not matched_cid and len(name_to_ids.get(cf_name, set())) == 1:
                candidate_id = next(iter(name_to_ids[cf_name]))
                candidate_node = existing_nodes.get(candidate_id, {})
                if are_labels_compatible(candidate_node.get("label"), clean_label):
                    matched_cid = candidate_id

            # Priority 4: Alphanumeric stripped match with label compatibility check
            if not matched_cid and s_name and len(stripped_to_ids.get(s_name, set())) == 1:
                candidate_id = next(iter(stripped_to_ids[s_name]))
                candidate_node = existing_nodes.get(candidate_id, {})
                if are_labels_compatible(candidate_node.get("label"), clean_label):
                    matched_cid = candidate_id

            if matched_cid:
                # Entity exists in graph! Reuse existing canonical entity identity.
                add_lookup(clean_label, clean_name, matched_cid)
                if s_name:
                    stripped_key = f"{clean_label.casefold()}::{s_name}"
                    name_lookup_mapping[stripped_key] = matched_cid
                if matched_cid in resolved_canonical:
                    target = resolved_canonical[matched_cid]
                    if chunk_id and chunk_id not in target.evidence_chunk_ids:
                        target.evidence_chunk_ids.append(chunk_id)
                else:
                    node_data = existing_nodes.get(matched_cid, {})
                    resolved_canonical[matched_cid] = CanonicalEntity(
                        canonical_id=matched_cid,
                        label=node_data.get("label") or clean_label,
                        name=node_data.get("name") or clean_name,
                        aliases=list(node_data.get("aliases") or []),
                        evidence_chunk_ids=[chunk_id] if chunk_id else [],
                        embedding=node_data.get("embedding"),
                    )
            else:
                # Entity does NOT exist in code graph: create new ADR-originated Entity
                new_cid = EntityCanonicalizer.canonical_id(
                    label=clean_label, name=clean_name, scope=repository_id
                )
                name_to_ids.setdefault(cf_name, set()).add(new_cid)
                if s_name:
                    stripped_to_ids.setdefault(s_name, set()).add(new_cid)
                if n_key:
                    key_to_id[n_key] = new_cid
                add_lookup(clean_label, clean_name, new_cid)
                if s_name:
                    name_lookup_mapping[f"{clean_label.casefold()}::{s_name}"] = new_cid
                canonicalizer.alias_registry.add(clean_label, clean_name, new_cid)

                if new_cid in resolved_canonical:
                    target = resolved_canonical[new_cid]
                    if chunk_id and chunk_id not in target.evidence_chunk_ids:
                        target.evidence_chunk_ids.append(chunk_id)
                else:
                    new_entity = CanonicalEntity(
                        canonical_id=new_cid,
                        label=clean_label,
                        name=clean_name,
                        properties={
                            "source": "ADR",
                            "source_id": adr_id,
                        },
                        aliases=[clean_name],
                        evidence_chunk_ids=[chunk_id] if chunk_id else [],
                    )
                    resolved_canonical[new_cid] = new_entity
                    new_entities_to_embed.append(new_entity)

        # Generate embeddings for newly created ADR entities
        if new_entities_to_embed and self.embedder:
            await self._embed_new_entities(new_entities_to_embed)

        return resolved_canonical, name_lookup_mapping

    async def _embed_new_entities(self, entities: list[CanonicalEntity]) -> None:
        """Generate vector embeddings for newly discovered ADR entities."""
        texts = [
            f"Entity name: {e.name}\nEntity label: {e.label}\nSource: ADR"
            for e in entities
        ]
        try:
            vectors = await self.embedder.embed(texts)
            if len(vectors) != len(entities):
                raise RuntimeError(
                    f"Embedder returned {len(vectors)} vectors for {len(entities)} entities."
                )
            for entity, vector in zip(entities, vectors):
                if vector is not None and len(vector) == self.embedding_dimensions:
                    entity.embedding = [float(val) for val in vector]
                else:
                    raise RuntimeError(
                        f"Entity vector length {len(vector) if vector else 0} does not match expected dimensions {self.embedding_dimensions}."
                    )
        except Exception as exc:
            logger.exception(f"Failed to generate embeddings for ADR entities: {exc}")
            raise RuntimeError(f"Entity embedding generation failed: {exc}") from exc
