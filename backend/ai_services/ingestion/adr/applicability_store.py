"""Repository-scoped reads and guarded, applicability-only enrichment writes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_services.ingestion.adr.applicability import ApplicabilityMetadata
from ai_services.ingestion.adr.models import ADRChunk
from ai_services.models.adr_document import ADRDocument, ADRSection


@dataclass
class ADRRequirement:
    id: str
    kind: str
    adr_id: str
    title: str
    description: str
    evidence_chunk_ids: list[str]
    properties: dict
    adr_hash: str

    @property
    def applicability(self) -> ApplicabilityMetadata:
        return ApplicabilityMetadata.from_json(
            self.properties.get("applicability_json")
        )


class ADRApplicabilityStore:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def list_requirements(self, repository_id: str) -> list[ADRRequirement]:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (adr:ADR {repository: $repo})-[:DEFINES]->(n {repository: $repo})
            WHERE n:ArchitecturalDecision OR n:ArchitecturalConstraint
            OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk {repository: $repo})-[:SUPPORTS]->(n)
            RETURN n.id AS id, CASE WHEN n:ArchitecturalDecision THEN 'decision'
                   ELSE 'constraint' END AS kind, adr.id AS adr_id,
                   coalesce(n.title, '') AS title, coalesce(n.description, '') AS description,
                   collect(DISTINCT ch.id) AS evidence_chunk_ids,
                   properties(n) AS properties, adr.content_hash AS adr_hash
            ORDER BY adr_id, id
            """,
            repo=repository_id,
            database_=self.database,
        )
        requirements = [ADRRequirement(**dict(r)) for r in records]
        if len({r.id for r in requirements}) != len(requirements):
            raise ValueError("Requirement identity is ambiguous across source ADRs")
        return requirements

    def load_document(
        self, repository_id: str, adr_id: str
    ) -> tuple[ADRDocument, list[ADRChunk]]:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (adr:ADR {id: $adr_id, repository: $repo})
            OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk {repository: $repo})
            RETURN properties(adr) AS adr, properties(ch) AS chunk
            ORDER BY ch.chunkIndex, ch.id
            """,
            repo=repository_id,
            adr_id=adr_id,
            database_=self.database,
        )
        if not records:
            raise ValueError("Source ADR no longer exists in this repository")
        source = dict(records[0]["adr"])
        chunks = []
        for r in records:
            c = r["chunk"]
            if not c:
                continue
            chunks.append(
                ADRChunk(
                    chunk_id=c["id"],
                    adr_id=adr_id,
                    repository_id=repository_id,
                    section=c.get("section") or "General",
                    chunk_index=c.get("chunkIndex") or 0,
                    text=c.get("text") or "",
                    content_hash=c.get("contentHash") or "",
                    metadata={"section_level": c.get("sectionLevel") or 1},
                )
            )
        doc = ADRDocument(
            adr_id=adr_id,
            repository_id=repository_id,
            title=source.get("title") or "Untitled ADR",
            content="\n\n".join(c.text for c in chunks),
            sections=[
                ADRSection(
                    heading=c.section,
                    content=c.text,
                    level=c.metadata["section_level"],
                )
                for c in chunks
            ],
            source_type=source.get("source_type") or "MANUAL_UPLOAD",
            source_name=source.get("source_name") or "",
            source_id=source.get("source_id"),
            source_url=source.get("source_url"),
            source_version=source.get("source_version"),
            file_path="",
            content_hash=source.get("content_hash") or "",
        )
        return doc, chunks

    def list_candidates(
        self,
        repository_id: str,
        *,
        entity_ids: Sequence[str] = (),
        community_ids: Sequence[str] = (),
        languages: Sequence[str] = (),
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        """Rank hints, never exclude by inferred scope. Paginate to visit every requirement."""
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid candidate pagination")
        records, _, _ = self.driver.execute_query(
            """
            MATCH (n {repository: $repo})
            WHERE n:ArchitecturalDecision OR n:ArchitecturalConstraint
            WITH n,
                CASE WHEN any(x IN coalesce(n.applicability_entity_ids, []) WHERE x IN $entity_ids)
                     THEN 4 ELSE 0 END +
                CASE WHEN any(x IN coalesce(n.applicability_community_ids, []) WHERE x IN $community_ids)
                     THEN 3 ELSE 0 END +
                CASE WHEN coalesce(n.applicability_repository_wide, false) THEN 2 ELSE 0 END +
                CASE WHEN any(x IN coalesce(n.applicability_languages, []) WHERE x IN $languages)
                     THEN 1 ELSE 0 END +
                CASE WHEN n.applicability_json IS NULL OR coalesce(n.applicability_unresolved, true)
                     THEN 1 ELSE 0 END AS hint_score
            RETURN properties(n) AS properties, hint_score
            ORDER BY hint_score DESC, n.id SKIP $offset LIMIT $limit
            """,
            repo=repository_id,
            entity_ids=list(entity_ids),
            community_ids=list(community_ids),
            languages=list(languages),
            offset=offset,
            limit=limit,
            database_=self.database,
        )
        result = []
        for r in records:
            row = dict(r)
            try:
                row["applicability"] = ApplicabilityMetadata.from_json(
                    row["properties"].get("applicability_json"),
                )
            except ValueError:
                # Corrupt/future metadata is still discoverable; callers can inspect the raw value.
                row["applicability"] = ApplicabilityMetadata()
                row["metadata_error"] = True
            result.append(row)
        return result

    def update_batch(
        self,
        repository_id: str,
        requirements: Sequence[ADRRequirement],
        chunks: Sequence[ADRChunk],
        properties: dict[str, dict],
    ) -> None:
        if set(properties) != {r.id for r in requirements}:
            raise ValueError("Update IDs must match the supplied requirements exactly")
        if any(
            not key.startswith("applicability_")
            for p in properties.values()
            for key in p
        ):
            raise ValueError("Only applicability properties may be updated")
        for p in properties.values():
            if "applicability_json" in p:
                ApplicabilityMetadata.from_json(p["applicability_json"])
        payload = [
            {
                "id": r.id,
                "kind": r.kind,
                "adr_id": r.adr_id,
                "title": r.title,
                "description": r.description,
                "adr_hash": r.adr_hash,
                "evidence": sorted(r.evidence_chunk_ids),
                "previous_json": r.properties.get("applicability_json"),
                "properties": properties[r.id],
            }
            for r in requirements
        ]
        # The node lock is acquired before the compare-and-set read. Locks and all writes
        # roll back if any requirement/chunk changed or disappeared during the LLM call.
        with (
            self.driver.session(database=self.database) as session,
            session.begin_transaction() as tx,
        ):
            result = tx.run(
                """
                UNWIND $items AS item
                MATCH (adr:ADR {id: item.adr_id, repository: $repo})-[:DEFINES]->(n {id: item.id, repository: $repo})
                WHERE (item.kind = 'decision' AND n:ArchitecturalDecision)
                   OR (item.kind = 'constraint' AND n:ArchitecturalConstraint)
                SET n.applicability_write_lock = true
                WITH adr, n, item
                OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk {repository: $repo})-[:SUPPORTS]->(n)
                WITH adr, n, item, collect(DISTINCT ch.id) AS evidence
                WHERE coalesce(n.title, '') = item.title AND coalesce(n.description, '') = item.description
                  AND coalesce(adr.content_hash, '') = coalesce(item.adr_hash, '')
                  AND size(evidence) = size(item.evidence) AND all(x IN evidence WHERE x IN item.evidence)
                  AND ((n.applicability_json IS NULL AND item.previous_json IS NULL)
                       OR n.applicability_json = item.previous_json)
                SET n += item.properties
                REMOVE n.applicability_write_lock
                RETURN n.id AS id
                """,
                items=payload,
                repo=repository_id,
            )
            updated = {r["id"] for r in result}
            if updated != set(properties):
                raise RuntimeError(
                    "Requirement changed during enrichment; retry from fresh source data"
                )
            for chunk in chunks:
                result = tx.run(
                    """
                    MATCH (adr:ADR {id: $adr_id, repository: $repo})-[:HAS_CHUNK]->(ch:Chunk {id: $id, repository: $repo})
                    WHERE ch.text = $text AND coalesce(ch.contentHash, '') = $hash
                    RETURN ch.id AS id
                    """,
                    adr_id=chunk.adr_id,
                    repo=repository_id,
                    id=chunk.chunk_id,
                    text=chunk.text,
                    hash=chunk.content_hash,
                )
                if not list(result):
                    raise RuntimeError("Source chunk changed during enrichment; retry")
            tx.commit()
