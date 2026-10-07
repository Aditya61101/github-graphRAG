from __future__ import annotations

from hashlib import sha256
import logging
from typing import Any, Sequence

from ai_services.embeddings.base import Embedder
from ai_services.ingestion.adr.chunker import ADRChunker
from ai_services.ingestion.adr.extractor import ADRArchitecturalExtractor
from ai_services.ingestion.adr.models import (
    ADRChunk,
    ArchitecturalConstraintNode,
    ArchitecturalDecisionNode,
)
from ai_services.ingestion.adr.neo4j_writer import ADRNeo4jWriter
from ai_services.ingestion.adr.resolver import ADREntityResolver
from ai_services.models.adr_document import ADRDocument

logger = logging.getLogger(__name__)


class ADRProcessingError(Exception):
    """Raised when an error occurs during the ADR Phase 2 pipeline."""
    pass


class ADRProcessingService:
    """Coordinates ADR chunking, embedding, architectural extraction, entity resolution, and Neo4j persistence."""

    def __init__(
        self,
        chunker: ADRChunker,
        extractor: ADRArchitecturalExtractor,
        resolver: ADREntityResolver,
        writer: ADRNeo4jWriter,
        embedder: Embedder,
        embedding_dimensions: int = 3072,
    ) -> None:
        self.chunker = chunker
        self.extractor = extractor
        self.resolver = resolver
        self.writer = writer
        if embedder is None:
            raise ValueError("ADRProcessingService requires an embedder for Phase 2")
        self.embedder = embedder
        self.embedding_dimensions = embedding_dimensions

    async def process_adr(self, doc: ADRDocument) -> None:
        """Execute complete Phase 2 knowledge graph transformation and persistence."""
        adr_id = doc.adr_id
        repo_id = doc.repository_id
        logger.info(
            f"Phase 2 ADR processing started for '{adr_id}' in repository '{repo_id}' (title='{doc.title}')"
        )

        try:
            # 1. Chunk document
            chunks = self.chunker.chunk(doc)
            logger.info(f"ADR '{adr_id}' chunked into {len(chunks)} structural chunks.")

            # 2. Embed chunks
            if chunks:
                chunk_texts = [c.text for c in chunks]
                embeddings = await self.embedder.embed(chunk_texts)
                if len(embeddings) != len(chunks):
                    raise RuntimeError(
                        f"Chunk embedding count mismatch ({len(embeddings)} != {len(chunks)})"
                    )
                for chunk, vector in zip(chunks, embeddings):
                    if vector is None or len(vector) != self.embedding_dimensions:
                        actual = 0 if vector is None else len(vector)
                        raise RuntimeError(
                            f"Chunk embedding dimension mismatch ({actual} != {self.embedding_dimensions})"
                        )
                    chunk.embedding = [float(v) for v in vector]

            # 3. LLM Architectural Extraction
            extraction_results = await self.extractor.extract_all(chunks)
            logger.info(
                f"LLM architectural extraction completed for {len(chunks)} chunks of ADR '{adr_id}'."
            )

            # 4. Aggregate extracted decisions, constraints, entities, and relationships
            raw_entities: list[tuple[str, str, str]] = []  # (name, label, chunk_id)
            chunk_mentions_pre: list[tuple[str, str, str]] = []  # (chunk_id, entity_name, entity_label)
            unmapped_decisions: list[tuple[int, str, Any, ADRChunk]] = []
            unmapped_constraints: list[tuple[Any, ADRChunk]] = []
            unmapped_relationships: list[tuple[Any, ADRChunk]] = []

            for chunk, result in extraction_results:
                # Entities explicitly identified
                for entity in result.entities:
                    raw_entities.append((entity.name, entity.label, chunk.chunk_id))
                    chunk_mentions_pre.append((chunk.chunk_id, entity.name, entity.label))

                # Decisions
                for idx, decision in enumerate(result.decisions):
                    unmapped_decisions.append((idx, chunk.chunk_id, decision, chunk))
                    for aff in decision.affects:
                        raw_entities.append((aff, "Component", chunk.chunk_id))
                        chunk_mentions_pre.append((chunk.chunk_id, aff, "Component"))
                    for con in decision.constrains:
                        raw_entities.append((con, "Component", chunk.chunk_id))
                        chunk_mentions_pre.append((chunk.chunk_id, con, "Component"))

                # Constraints
                for constraint in result.constraints:
                    unmapped_constraints.append((constraint, chunk))
                    for target in constraint.target_entities:
                        raw_entities.append((target, "Component", chunk.chunk_id))
                        chunk_mentions_pre.append((chunk.chunk_id, target, "Component"))

                # Relationships
                for rel in result.relationships:
                    unmapped_relationships.append((rel, chunk))
                    raw_entities.append((rel.source_name, rel.source_label, chunk.chunk_id))
                    raw_entities.append((rel.target_name, rel.target_label, chunk.chunk_id))
                    chunk_mentions_pre.append((chunk.chunk_id, rel.source_name, rel.source_label))
                    chunk_mentions_pre.append((chunk.chunk_id, rel.target_name, rel.target_label))

            # 5. Entity Canonicalization & Resolution against existing code graph
            canonical_map, name_to_cid = await self.resolver.resolve_entities(
                repository_id=repo_id,
                adr_id=adr_id,
                raw_entities=raw_entities,
            )
            logger.info(
                f"Entity resolution resolved {len(canonical_map)} canonical entities for ADR '{adr_id}'."
            )

            def lookup_entity(name: str, label: str | None = None) -> str | None:
                clean_name = name.strip().casefold()
                if not clean_name:
                    return None
                if label:
                    exact = name_to_cid.get(f"{label.strip().casefold()}::{clean_name}")
                    if exact:
                        return exact
                return name_to_cid.get(clean_name)

            # 6. Map decisions to canonical entities
            decision_nodes: list[ArchitecturalDecisionNode] = []
            for idx, chunk_id, dec, chunk in unmapped_decisions:
                dec_id = sha256(f"{adr_id}:decision:{idx}:{dec.title}".encode()).hexdigest()
                aff_cids = [
                    lookup_entity(name, "Component")
                    for name in dec.affects
                    if lookup_entity(name, "Component")
                ]
                con_cids = [
                    lookup_entity(name, "Component")
                    for name in dec.constrains
                    if lookup_entity(name, "Component")
                ]
                decision_nodes.append(
                    ArchitecturalDecisionNode(
                        id=dec_id,
                        adr_id=adr_id,
                        repository_id=repo_id,
                        title=dec.title,
                        description=dec.description,
                        decision_type=dec.decision_type,
                        affects_entity_ids=list(dict.fromkeys(aff_cids)),
                        constrains_entity_ids=list(dict.fromkeys(con_cids)),
                        evidence_chunk_ids=[chunk_id],
                    )
                )

            # 7. Map ArchitecturalConstraint nodes and direct ADR constraints
            constraint_nodes: list[ArchitecturalConstraintNode] = []
            adr_constraints: list[str] = []
            for idx, (constr, chunk) in enumerate(unmapped_constraints):
                constr_id = sha256(f"{adr_id}:constraint:{idx}:{constr.description}".encode()).hexdigest()
                target_cids: list[str] = []
                for target in constr.target_entities:
                    cid = lookup_entity(target, "Component")
                    if cid and cid not in target_cids:
                        target_cids.append(cid)
                    if cid and cid not in adr_constraints:
                        adr_constraints.append(cid)
                constraint_nodes.append(
                    ArchitecturalConstraintNode(
                        id=constr_id,
                        adr_id=adr_id,
                        repository_id=repo_id,
                        description=constr.description,
                        constraint_type=constr.constraint_type,
                        constrains_entity_ids=target_cids,
                        evidence_chunk_ids=[chunk.chunk_id],
                    )
                )

            # 8. Map relationships
            mapped_relationships: list[dict[str, Any]] = []
            for rel, chunk in unmapped_relationships:
                src_cid = lookup_entity(rel.source_name, rel.source_label)
                tgt_cid = lookup_entity(rel.target_name, rel.target_label)
                if src_cid and tgt_cid and src_cid != tgt_cid:
                    mapped_relationships.append(
                        {
                            "source_id": src_cid,
                            "target_id": tgt_cid,
                            "relationship_type": rel.relationship_type,
                            "rationale": rel.rationale,
                            "evidence": [chunk.chunk_id],
                        }
                    )

            # 9. Map chunk mentions: (chunk_id, canonical_id)
            chunk_mentions: list[tuple[str, str]] = []
            seen_mentions: set[tuple[str, str]] = set()
            for chunk_id, entity_name, entity_label in chunk_mentions_pre:
                cid = lookup_entity(entity_name, entity_label)
                if cid and (chunk_id, cid) not in seen_mentions:
                    seen_mentions.add((chunk_id, cid))
                    chunk_mentions.append((chunk_id, cid))

            # 10. Persist full ADR graph atomically into Neo4j
            self.writer.write_adr_knowledge(
                doc=doc,
                chunks=chunks,
                canonical_entities=list(canonical_map.values()),
                chunk_mentions=chunk_mentions,
                decisions=decision_nodes,
                constraints=constraint_nodes,
                adr_constraints=adr_constraints,
                relationships=mapped_relationships,
            )
            logger.info(
                f"Phase 2 ADR processing successfully persisted into Neo4j for '{adr_id}'."
            )

        except Exception as exc:
            logger.exception(f"Phase 2 ADR processing failed for '{adr_id}': {exc}")
            raise ADRProcessingError(f"ADR graph ingestion failed: {exc}") from exc
