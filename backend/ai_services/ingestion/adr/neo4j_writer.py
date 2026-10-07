from __future__ import annotations

import logging
from typing import Any, Sequence

from ai_services.ingestion.adr.models import (
    ADRChunk,
    ArchitecturalConstraintNode,
    ArchitecturalDecisionNode,
)
from ai_services.ingestion.rkg.models import CanonicalEntity
from ai_services.ingestion.rkg.neo4j_writer import sanitize_relationship_type
from ai_services.models.adr_document import ADRDocument

logger = logging.getLogger(__name__)


class ADRNeo4jWriter:
    """Safely persists ADR documents, chunks, architectural decisions, and relationships into Neo4j."""

    def __init__(self, driver: Any, database: str = "neo4j", embedding_dimensions: int = 3072) -> None:
        self.driver = driver
        self.database = database
        self.embedding_dimensions = embedding_dimensions

    def initialize_constraints(self) -> None:
        """Create constraints and indexes for ADRs, ArchitecturalDecisions, and ArchitecturalConstraints."""
        if not self.driver:
            return

        queries = [
            "CREATE CONSTRAINT adr_id IF NOT EXISTS FOR (n:ADR) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (n:ArchitecturalDecision) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT constraint_id IF NOT EXISTS FOR (n:ArchitecturalConstraint) REQUIRE n.id IS UNIQUE",
            "CREATE INDEX adr_repo_idx IF NOT EXISTS FOR (n:ADR) ON (n.repository)",
            "CREATE INDEX decision_repo_idx IF NOT EXISTS FOR (n:ArchitecturalDecision) ON (n.repository)",
            "CREATE INDEX constraint_repo_idx IF NOT EXISTS FOR (n:ArchitecturalConstraint) ON (n.repository)",
        ]
        for q in queries:
            try:
                self.driver.execute_query(q, database_=self.database)
            except Exception as exc:
                logger.exception("Failed to initialize required Neo4j schema: %s", exc)
                raise RuntimeError("Failed to initialize required ADR Neo4j schema") from exc

    def delete_stale_adr_knowledge(self, adr_id: str, repository_id: str) -> None:
        """Remove previously extracted chunks, decisions, constraints, and assertions for this ADR.

        Fails explicitly if deletion cannot be completed.
        """
        if not self.driver:
            return

        query = """
        MATCH (adr:ADR {id: $adr_id, repository: $repo})
        OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk {repository: $repo})
        OPTIONAL MATCH (adr)-[:DEFINES]->(d:ArchitecturalDecision {repository: $repo})
        OPTIONAL MATCH (adr)-[:DEFINES]->(ac:ArchitecturalConstraint {repository: $repo})
        OPTIONAL MATCH (a:GraphAssertion {source_id: $adr_id, repository: $repo})

        FOREACH (c IN CASE WHEN ch IS NOT NULL THEN [ch] ELSE [] END | DETACH DELETE c)
        FOREACH (dec IN CASE WHEN d IS NOT NULL THEN [d] ELSE [] END | DETACH DELETE dec)
        FOREACH (con IN CASE WHEN ac IS NOT NULL THEN [ac] ELSE [] END | DETACH DELETE con)
        FOREACH (asrt IN CASE WHEN a IS NOT NULL THEN [a] ELSE [] END | DETACH DELETE asrt)

        // Detach direct ADR relationships to entities
        WITH adr
        OPTIONAL MATCH (adr)-[r:AFFECTS|CONSTRAINS]->()
        DELETE r
        """
        try:
            self._run_query_or_tx(query, adr_id=adr_id, repo=repository_id)
        except Exception as exc:
            logger.exception(f"Failed to delete stale ADR knowledge for '{adr_id}': {exc}")
            raise RuntimeError(
                f"Failed to delete stale ADR knowledge for '{adr_id}': {exc}"
            ) from exc

    def _run_query_or_tx(self, query: str, tx: Any = None, **params) -> None:
        """Execute query either on an active transaction or directly via execute_query."""
        if tx is not None:
            tx.run(query, params)
        else:
            self.driver.execute_query(query, database_=self.database, **params)

    def write_adr_knowledge(
        self,
        doc: ADRDocument,
        chunks: Sequence[ADRChunk],
        canonical_entities: Sequence[CanonicalEntity],
        chunk_mentions: Sequence[tuple[str, str]],  # (chunk_id, entity_id)
        decisions: Sequence[ArchitecturalDecisionNode],
        constraints: Sequence[ArchitecturalConstraintNode],
        adr_constraints: Sequence[str],  # constrained entity_ids
        relationships: Sequence[dict[str, Any]],  # source_id, target_id, rel_type, rationale, evidence
    ) -> None:
        """Persist full ADR graph transactionally within the existing Neo4j knowledge graph."""
        if not self.driver:
            return

        repo_id = doc.repository_id
        adr_id = doc.adr_id

        # Phase 2 requires transactional persistence. Never silently fall back to
        # independent auto-commit queries because that can leave a partially ingested ADR.
        if not hasattr(self.driver, "session"):
            raise RuntimeError("Neo4j driver must support explicit sessions/transactions for ADR ingestion")

        with self.driver.session(database=self.database) as session:
            with session.begin_transaction() as tx:
                self._persist_adr_in_tx(
                    tx=tx,
                    doc=doc,
                    chunks=chunks,
                    canonical_entities=canonical_entities,
                    chunk_mentions=chunk_mentions,
                    decisions=decisions,
                    constraints=constraints,
                    adr_constraints=adr_constraints,
                    relationships=relationships,
                )
                tx.commit()

    def _persist_adr_in_tx(
        self,
        tx: Any,
        doc: ADRDocument,
        chunks: Sequence[ADRChunk],
        canonical_entities: Sequence[CanonicalEntity],
        chunk_mentions: Sequence[tuple[str, str]],
        decisions: Sequence[ArchitecturalDecisionNode],
        constraints: Sequence[ArchitecturalConstraintNode],
        adr_constraints: Sequence[str],
        relationships: Sequence[dict[str, Any]],
    ) -> None:
        repo_id = doc.repository_id
        adr_id = doc.adr_id

        # 1. Clean up stale knowledge from prior runs of this ADR (Idempotency / update support)
        # Deleting within the same transaction ensures all-or-nothing atomicity
        stale_delete_query = """
        MATCH (adr:ADR {id: $adr_id, repository: $repo})
        OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk {repository: $repo})
        OPTIONAL MATCH (adr)-[:DEFINES]->(d:ArchitecturalDecision {repository: $repo})
        OPTIONAL MATCH (adr)-[:DEFINES]->(ac:ArchitecturalConstraint {repository: $repo})
        OPTIONAL MATCH (a:GraphAssertion {source_id: $adr_id, repository: $repo})

        FOREACH (c IN CASE WHEN ch IS NOT NULL THEN [ch] ELSE [] END | DETACH DELETE c)
        FOREACH (dec IN CASE WHEN d IS NOT NULL THEN [d] ELSE [] END | DETACH DELETE dec)
        FOREACH (con IN CASE WHEN ac IS NOT NULL THEN [ac] ELSE [] END | DETACH DELETE con)
        FOREACH (asrt IN CASE WHEN a IS NOT NULL THEN [a] ELSE [] END | DETACH DELETE asrt)

        WITH adr
        OPTIONAL MATCH (adr)-[r:AFFECTS|CONSTRAINS]->()
        DELETE r
        """
        self._run_query_or_tx(stale_delete_query, tx=tx, adr_id=adr_id, repo=repo_id)

        # 2. Upsert ADR node
        adr_query = """
        MERGE (adr:ADR {id: $adr_id})
        SET adr.repository = $repo,
            adr.repository_id = $repo,
            adr.title = $title,
            adr.description = $description,
            adr.status = 'COMPLETED',
            adr.source_type = $source_type,
            adr.source_id = $source_id,
            adr.source_name = $source_name,
            adr.source_url = $source_url,
            adr.source_version = $source_version,
            adr.content_hash = $content_hash,
            adr.created_at = coalesce(adr.created_at, datetime().epochMillis),
            adr.updated_at = datetime().epochMillis
        """
        self._run_query_or_tx(
            adr_query,
            tx=tx,
            adr_id=adr_id,
            repo=repo_id,
            title=doc.title,
            description=doc.metadata.get("description") or None,
            source_type=doc.source_type,
            source_id=doc.source_id,
            source_name=doc.source_name,
            source_url=doc.source_url,
            source_version=doc.source_version,
            content_hash=doc.content_hash,
        )

        # 3. Upsert Chunks with dual labels (:Chunk:ADRChunk) for seamless vector search
        if chunks:
            chunk_payload = [
                {
                    "id": c.chunk_id,
                    "section": c.section,
                    "chunk_index": c.chunk_index,
                    "text": c.text,
                    "content_hash": c.content_hash,
                    "embedding": c.embedding,
                }
                for c in chunks
            ]
            chunks_query = """
            MATCH (adr:ADR {id: $adr_id, repository: $repo})
            UNWIND $chunks AS item
            MERGE (c:Chunk:ADRChunk {id: item.id})
            SET c.repository = $repo,
                c.adr_id = $adr_id,
                c.section = item.section,
                c.chunkIndex = item.chunk_index,
                c.text = item.text,
                c.contentHash = item.content_hash,
                c.strategy = 'adr_section'
            FOREACH (_ IN CASE WHEN item.embedding IS NOT NULL THEN [1] ELSE [] END |
                SET c.embedding = item.embedding
            )
            MERGE (adr)-[:HAS_CHUNK]->(c)
            """
            self._run_query_or_tx(
                chunks_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                chunks=chunk_payload,
            )

        # 4. Upsert newly discovered ADR entities (preserving code graph entities)
        adr_originated_entities = [
            e for e in canonical_entities
            if e.properties.get("source") == "ADR"
        ]
        if adr_originated_entities:
            entity_payload = [
                {
                    "id": e.canonical_id,
                    "name": e.name,
                    "label": e.label,
                    "aliases": list(e.aliases),
                    "embedding": e.embedding,
                }
                for e in adr_originated_entities
            ]
            entities_query = """
            UNWIND $entities AS item
            MERGE (e:Entity {id: item.id})
            SET e.name = item.name,
                e.label = item.label,
                e.repository = $repo,
                e.source = 'ADR',
                e.source_id = $adr_id,
                e.aliases = item.aliases
            FOREACH (_ IN CASE WHEN item.embedding IS NOT NULL THEN [1] ELSE [] END |
                SET e.embedding = item.embedding
            )
            """
            self._run_query_or_tx(
                entities_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                entities=entity_payload,
            )

        # 5. Chunk mentions: (Chunk)-[:MENTIONS]->(Entity)
        if chunk_mentions:
            mentions_query = """
            UNWIND $mentions AS item
            MATCH (c:Chunk {id: item.chunk_id, repository: $repo})
            MATCH (e:Entity {id: item.entity_id, repository: $repo})
            MERGE (c)-[:MENTIONS]->(e)
            """
            self._run_query_or_tx(
                mentions_query,
                tx=tx,
                repo=repo_id,
                mentions=[{"chunk_id": c, "entity_id": e} for c, e in chunk_mentions],
            )

        # 6. ArchitecturalDecisions
        if decisions:
            decisions_payload = [
                {
                    "id": d.id,
                    "title": d.title,
                    "description": d.description,
                    "decision_type": d.decision_type or "general_decision",
                    "affects": d.affects_entity_ids,
                    "constrains": d.constrains_entity_ids,
                    "evidence": d.evidence_chunk_ids,
                }
                for d in decisions
            ]
            decisions_query = """
            MATCH (adr:ADR {id: $adr_id, repository: $repo})
            UNWIND $decisions AS item
            MERGE (d:ArchitecturalDecision {id: item.id})
            SET d.repository = $repo,
                d.adr_id = $adr_id,
                d.title = item.title,
                d.description = item.description,
                d.decision_type = item.decision_type,
                d.created_at = datetime().epochMillis
            MERGE (adr)-[:DEFINES]->(d)

            WITH adr, d, item
            OPTIONAL MATCH (c:Chunk {repository: $repo, adr_id: $adr_id})
            WHERE c.id IN item.evidence
            FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END |
                MERGE (c)-[:SUPPORTS]->(d)
            )
            WITH adr, d, item
            FOREACH (aff_id IN item.affects |
                MERGE (e_aff:Entity {id: aff_id, repository: $repo})
                MERGE (d)-[:AFFECTS]->(e_aff)
                MERGE (adr)-[:AFFECTS]->(e_aff)
            )

            WITH adr, d, item
            FOREACH (con_id IN item.constrains |
                MERGE (e_con:Entity {id: con_id, repository: $repo})
                MERGE (d)-[:CONSTRAINS]->(e_con)
                MERGE (adr)-[:CONSTRAINS]->(e_con)
            )
            """
            self._run_query_or_tx(
                decisions_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                decisions=decisions_payload,
            )

        # 7. First-class ArchitecturalConstraint nodes
        if constraints:
            constraints_payload = [
                {
                    "id": c.id,
                    "description": c.description,
                    "constraint_type": c.constraint_type or "general_constraint",
                    "constrains": c.constrains_entity_ids,
                    "evidence": c.evidence_chunk_ids,
                }
                for c in constraints
            ]
            constraints_query = """
            MATCH (adr:ADR {id: $adr_id, repository: $repo})
            UNWIND $constraints AS item
            MERGE (c:ArchitecturalConstraint {id: item.id})
            SET c.repository = $repo,
                c.adr_id = $adr_id,
                c.description = item.description,
                c.constraint_type = item.constraint_type,
                c.created_at = datetime().epochMillis
            MERGE (adr)-[:DEFINES]->(c)

            WITH adr, c, item
            OPTIONAL MATCH (chunk:Chunk {repository: $repo, adr_id: $adr_id})
            WHERE chunk.id IN item.evidence
            FOREACH (_ IN CASE WHEN chunk IS NOT NULL THEN [1] ELSE [] END |
                MERGE (chunk)-[:SUPPORTS]->(c)
            )
            WITH adr, c, item
            FOREACH (con_id IN item.constrains |
                MERGE (e_con:Entity {id: con_id, repository: $repo})
                MERGE (c)-[:CONSTRAINS]->(e_con)
                MERGE (adr)-[:CONSTRAINS]->(e_con)
            )
            """
            self._run_query_or_tx(
                constraints_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                constraints=constraints_payload,
            )

        # 8. Direct ADR Constraints
        if adr_constraints:
            direct_constraints_query = """
            MATCH (adr:ADR {id: $adr_id, repository: $repo})
            UNWIND $target_ids AS tid
            MATCH (e:Entity {id: tid, repository: $repo})
            MERGE (adr)-[:CONSTRAINS]->(e)
            """
            self._run_query_or_tx(
                direct_constraints_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                target_ids=list(set(adr_constraints)),
            )

        # 9. Entity Relationships with ADR-Scoped GraphAssertion provenance
        if relationships:
            assertion_query = """
            UNWIND $relationships AS item
            MATCH (s:Entity {id: item.source_id, repository: $repo})
            MATCH (t:Entity {id: item.target_id, repository: $repo})

            MERGE (a:GraphAssertion {id: item.assertion_id})
            SET a.repository = $repo,
                a.source_type = 'ADR',
                a.source_id = $adr_id,
                a.relationshipType = item.relationship_type,
                a.confidence = 1.0,
                a.rationale = item.rationale

            MERGE (s)-[:ASSERTS]->(a)
            MERGE (a)-[:TARGETS]->(t)

            WITH a, item
            UNWIND item.evidence AS chunk_id
            MATCH (c:Chunk {id: chunk_id, repository: $repo})
            MERGE (c)-[:SUPPORTS]->(a)
            """
            assertions_payload = [
                {
                    "source_id": r["source_id"],
                    "target_id": r["target_id"],
                    "relationship_type": sanitize_relationship_type(r["relationship_type"]),
                    # ADR-scoped assertion ID prevents clashing between multiple ADRs or code assertions
                    "assertion_id": (
                        f"adr:{adr_id}:{r['source_id']}|{sanitize_relationship_type(r['relationship_type'])}|{r['target_id']}"
                    ),
                    "rationale": r.get("rationale") or "Extracted from ADR",
                    "evidence": r.get("evidence") or [],
                }
                for r in relationships
            ]
            self._run_query_or_tx(
                assertion_query,
                tx=tx,
                adr_id=adr_id,
                repo=repo_id,
                relationships=assertions_payload,
            )

            # ADR-derived entity relationships are represented authoritatively by
            # GraphAssertion + provenance/evidence. Do not create direct Entity->Entity
            # edges here: those edges are part of the accepted/current code architecture
            # and cannot safely represent multiple ADR sources or stale-knowledge cleanup.
