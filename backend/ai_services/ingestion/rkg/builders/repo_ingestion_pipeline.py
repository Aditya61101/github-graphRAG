from __future__ import annotations

import os
from pathlib import Path

from chunker import LanguageDetectorImpl
from neo4j import Driver

from ai_services.ingestion.rkg.ingestion_pipeline import RepositoryIngestionPipeline
from ai_services.ingestion.rkg.neo4j_writer import Neo4jRepositoryWriter
from ai_services.ingestion.rkg.splitters import RepositoryTextSplitter
from .knowledge_pipeline import build_knowledge_pipeline


def build_repository_ingestion_pipeline(
    repository_root: Path,
    repository_id: str,
    repository_name: str,
    commit: str,
    neo4j_driver: Driver,
    candidates_path: str | Path,
    full_name: str | None = None,
    owner: str | None = None,
    examples: str = "",
    progress_callback=None,
    publication_guard=None,
):
    database = os.environ.get("NEO4J_DATABASE", "neo4j")

    knowledge_pipeline = build_knowledge_pipeline(
        candidates_path=candidates_path,
        examples=examples,
    )

    splitter = RepositoryTextSplitter()
    neo4j_writer = Neo4jRepositoryWriter(
        driver=neo4j_driver,
        database=database,
    )

    async def graph_neighborhood_loader(entity_id: str) -> str:
        result = neo4j_driver.execute_query(
            """
            MATCH (e:Entity {id: $entity_id, repository: $repo_id})
            OPTIONAL MATCH (e)-[r]-(n:Entity {repository: $repo_id})
            WHERE type(r) <> "MEMBER_OF"
            RETURN
                e.name AS source,
                type(r) AS relationship,
                n.name AS neighbor
            ORDER BY relationship, neighbor
            """,
            entity_id=entity_id,
            repo_id=repository_id,
            database_=database,
        )

        return "\n".join(
            f'{record["source"]} -[{record["relationship"]}]- {record["neighbor"]}'
            for record in result.records
            if record["relationship"] is not None
        )

    return RepositoryIngestionPipeline(
        repository_root=repository_root,
        repository_id=repository_id,
        repository_name=repository_name,
        commit=commit,
        splitter=splitter,
        knowledge_pipeline=knowledge_pipeline,
        cross_chunk_reasoner=knowledge_pipeline.cross_chunk_reasoner,
        neo4j_writer=neo4j_writer,
        graph_neighborhood_loader=graph_neighborhood_loader,
        language_detector=LanguageDetectorImpl(),
        full_name=full_name,
        owner=owner,
        progress_callback=progress_callback,
        publication_guard=publication_guard,
    )
