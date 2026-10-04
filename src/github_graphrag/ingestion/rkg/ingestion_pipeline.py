from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from github_graphrag.models.ingestion_plan import FilePlan, IngestionAction

from .cross_chunk import CrossChunkReasoner
from .models import ChunkStrategy, EvidenceChunk
from .neo4j_writer import Neo4jRepositoryWriter
from .pipeline import RepositoryKnowledgePipeline
from .splitters import RepositoryTextSplitter


@dataclass
class RepositoryIngestionResult:
    chunks: list[EvidenceChunk]
    candidate_knowledge: list[Any]
    entities: Any
    relationship_candidates: list[Any]
    validated_relationships: list[Any]


class RepositoryIngestionPipeline:
    """Top-level orchestrator from planner output to the authoritative graph."""

    def __init__(
        self,
        repository_root: Path,
        repository_name: str,
        commit: str,
        splitter: RepositoryTextSplitter,
        knowledge_pipeline: RepositoryKnowledgePipeline,
        cross_chunk_reasoner: CrossChunkReasoner,
        neo4j_writer: Neo4jRepositoryWriter,
        language_detector,
        graph_neighborhood_loader,
    ):
        self.repository_root = repository_root
        self.repository_name = repository_name
        self.commit = commit
        self.splitter = splitter
        self.knowledge_pipeline = knowledge_pipeline
        self.cross_chunk_reasoner = cross_chunk_reasoner
        self.neo4j_writer = neo4j_writer
        self.language_detector = language_detector
        self.graph_neighborhood_loader = graph_neighborhood_loader

    async def ingest(self, file_plans) -> RepositoryIngestionResult:
        chunks = self._create_chunks(file_plans)
        if not chunks:
            return RepositoryIngestionResult([], [], None, [], [])

        result = await self.knowledge_pipeline.process_chunks(chunks)
        processed_chunks = result["chunks"]
        candidate_knowledge = result["candidate_knowledge"]
        entities = result["entities"]
        relationship_candidates = result["relationship_candidates"]

        # Persist source chunks and canonical entities BEFORE relationship
        # validation so the validator can inspect the current graph context.
        self.neo4j_writer.initialize_constraints(
            embedding_dimensions=self.knowledge_pipeline.embedding_dimensions,
        )
        self.neo4j_writer.write_source(
            repository=self.repository_name,
            commit=self.commit,
            chunks=processed_chunks,
        )
        self.neo4j_writer.write_entities(entities.entities.values())

        chunks_by_id = {chunk.chunk_id: chunk for chunk in processed_chunks}
        
        print("=== RELATIONSHIP DEBUG ===")
        print("Extracted relationships:", sum(len(k.relationships) for k in candidate_knowledge))
        print("Relationship candidates:", len(relationship_candidates))
        
        validated_relationships = await self.cross_chunk_reasoner.validate_all(
            relationship_candidates,
            entities=entities,
            chunks_by_id=chunks_by_id,
            graph_neighborhood_loader=self.graph_neighborhood_loader,
        )
        
        print("Validated relationships:", len(validated_relationships))

        # for r in validated_relationships:
        #     print(
        #         r.source_id,
        #         r.relationship_type,
        #         r.target_id,
        #         r.confidence,
        #         r.rationale,
        #     )

        self.neo4j_writer.write_relationships(validated_relationships)

        return RepositoryIngestionResult(
            chunks=processed_chunks,
            candidate_knowledge=candidate_knowledge,
            entities=entities,
            relationship_candidates=relationship_candidates,
            validated_relationships=validated_relationships,
        )

    def _create_chunks(self, file_plans: list[FilePlan]) -> list[EvidenceChunk]:
        chunks: list[EvidenceChunk] = []

        for file_plan in file_plans:
            if file_plan.action == IngestionAction.EXCLUDE:
                continue

            file_path = self.repository_root / file_plan.path
            if not file_path.is_file():
                continue

            text = file_path.read_text(encoding="utf-8", errors="replace")
            strategy = ChunkStrategy(file_plan.chunk_strategy)

            language = None
            if strategy == ChunkStrategy.SYMBOL:
                language, _ = self.language_detector.detect_from_file(file_path)

            chunks.extend(
                self.splitter.split(
                    repository=self.repository_name,
                    commit=self.commit,
                    file_path=file_plan.path,
                    text=text,
                    strategy=strategy,
                    language=language,
                )
            )

        return chunks
