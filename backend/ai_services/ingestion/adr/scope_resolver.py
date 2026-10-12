"""Conservative, read-only scope associations; refreshable without LLM extraction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ai_services.ingestion.adr.applicability import Applicability, ApplicabilityMetadata
from ai_services.ingestion.adr.resolver import ADREntityResolver, _strip_name
from ai_services.ingestion.rkg.models import CanonicalEntity


class ADRScopeResolver:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def load_graph(self, repository_id: str) -> tuple[dict, dict]:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (e:Entity {repository: $repo})
            OPTIONAL MATCH (e)-[:MEMBER_OF]->(c:Community {repository: $repo})
            RETURN e.id AS id, e.name AS name, e.label AS label,
                   coalesce(e.aliases, []) AS aliases,
                   collect(DISTINCT c.communityId) AS community_ids
            """,
            repo=repository_id,
            database_=self.database,
        )
        entities = {r["id"]: dict(r) for r in records if r["id"]}
        records, _, _ = self.driver.execute_query(
            """
            MATCH (c:Community {repository: $repo})
            RETURN c.communityId AS id, c.name AS name, c.title AS title,
                   c.architecturalRole AS architectural_role,
                   c.summary AS summary, coalesce(c.aliases, []) AS aliases
            """,
            repo=repository_id,
            database_=self.database,
        )
        communities = {str(r["id"]): dict(r) for r in records if r["id"] is not None}
        return entities, communities

    @staticmethod
    def resolve(
        applicability: Applicability,
        entities: dict,
        communities: dict,
        *,
        context_truncated: bool = False,
    ) -> ApplicabilityMetadata:
        entity_ids: set[str] = set()
        community_ids: set[str] = set()
        unresolved: list[str] = []
        for target in applicability.targets:
            if target.basis == "uncertain" or not target.evidence_chunk_ids:
                unresolved.append(target.name)
                continue
            if target.kind == "entity" and "entity" in applicability.scopes:
                cid = ADREntityResolver.match_reference(
                    target.name, target.label, entities
                )
                if cid:
                    entity_ids.add(cid)
                else:
                    unresolved.append(target.name)
            elif (
                target.kind == "community"
                and "community" in applicability.scopes
                and target.governs_entire_scope
            ):
                # A graph's explicit subsystem name/alias is evidence for its identity.
                # Do not fuzzy-match summaries or convert an entity mention to community scope.
                name = _strip_name(target.name)
                matches = [
                    cid
                    for cid, c in communities.items()
                    if name
                    and name
                    in {
                        _strip_name(str(n))
                        for n in [
                            c.get("name"),
                            c.get("title"),
                            c.get("architectural_role"),
                            *(c.get("aliases") or []),
                        ]
                        if n
                    }
                ]
                for cid in matches:
                    if any(
                        cid in {str(v) for v in e.get("community_ids", [])}
                        for e in entities.values()
                    ):
                        community_ids.add(cid)
                if not any(cid in community_ids for cid in matches):
                    unresolved.append(target.name)
            else:
                unresolved.append(target.name)
        snapshot = hashlib.sha256(
            json.dumps(
                {"entities": entities, "communities": communities},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return ApplicabilityMetadata(
            applicability=applicability,
            entity_ids=sorted(entity_ids),
            community_ids=sorted(community_ids),
            unresolved_targets=sorted(set(unresolved)),
            graph_snapshot=snapshot,
            context_truncated=context_truncated,
        )

    def resolve_all(
        self,
        repository_id: str,
        nodes: Sequence[Any],
        canonical_entities: Sequence[CanonicalEntity] = (),
    ) -> None:
        # Existing graph state is read once for the entire document; no code relationships written.
        if not any(n.applicability.applicability.targets for n in nodes):
            return
        entities, communities = self.load_graph(repository_id)
        for e in canonical_entities:
            entities.setdefault(
                e.canonical_id,
                {
                    "id": e.canonical_id,
                    "name": e.name,
                    "label": e.label,
                    "aliases": e.aliases,
                    "community_ids": [],
                },
            )
        for node in nodes:
            node.applicability = self.resolve(
                node.applicability.applicability,
                entities,
                communities,
                context_truncated=node.applicability.context_truncated,
            )
