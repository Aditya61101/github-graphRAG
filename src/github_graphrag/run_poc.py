"""Extract a tiny, controlled Python codebase into Neo4j with GraphRAG 1.19.0.

This milestone intentionally stops after knowledge-graph construction and
inspection. It does not create a retrieval or answer-generation application.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Final
from dotenv import load_dotenv

from github_graphrag.embed_communities_summary import embed_communities
from github_graphrag.summarize_communities import generate_community_summary
load_dotenv()

from github_graphrag.graphdb_driver import CreateDriver
from github_graphrag.communities_operations import detect_communities, ensure_projection, get_communities, save_community
from github_graphrag.embed_entities import create_entity_embeddings

from neo4j_graphrag.embeddings import GeminiEmbedder
from neo4j_graphrag.exceptions import LLMGenerationError
from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline
from neo4j_graphrag.llm import GeminiLLM


ROOT: Final = Path(__file__).resolve().parent
SOURCE_DIR: Final = ROOT / "sample_code"
SOURCE_FILES: Final = ("database.py", "user_repository.py", "auth.py")

# A narrow schema prevents generic nodes from dominating this first extraction.
SCHEMA: Final = {
    "node_types": [
        {"label": "Service", "description": "An application service class."},
        {"label": "Repository", "description": "A data-access repository class."},
        {"label": "Database", "description": "A database or database-access component."},
        {"label": "DomainEntity", "description": "A business entity or record."},
        {"label": "Credential", "description": "A credential used for authentication."},
        {"label": "Token", "description": "A token issued after authentication."},
    ],
    "relationship_types": [
        {"label": "USES", "description": "One component uses another."},
        {"label": "AUTHENTICATES", "description": "A service authenticates an entity."},
        {"label": "READS_FROM", "description": "A repository reads an entity from a database."},
        {"label": "VALIDATES", "description": "A service validates a credential."},
        {"label": "ISSUES", "description": "A service issues a token."},
    ],
    "patterns": [
        ("Service", "USES", "Repository"),
        ("Repository", "USES", "Database"),
        ("Repository", "READS_FROM", "DomainEntity"),
        ("Service", "AUTHENTICATES", "DomainEntity"),
        ("Service", "VALIDATES", "Credential"),
        ("Service", "ISSUES", "Token"),
    ],
}


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required. Copy .env.example and set its value.")
    return value


def load_source() -> str:
    """Return the sample files with explicit path boundaries for the LLM."""
    parts = []
    for filename in SOURCE_FILES:
        path = SOURCE_DIR / filename
        parts.append(f"# FILE: {filename}\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts)


def inspect_graph(driver, database: str | None) -> None:
    """Print only GraphRAG-created entity nodes and their direct relationships."""
    records, _, _ = driver.execute_query(
        """
        MATCH (source:__Entity__)-[relationship]->(target:__Entity__)
        RETURN labels(source) AS source_labels,
            source.name AS source,
            type(relationship) AS relationship,
            labels(target) AS target_labels,
            target.name AS target
        ORDER BY source, relationship, target
        """,
        database_=database,
    )
    if not records:
        raise RuntimeError(
            "The pipeline completed but no entity-to-entity relationships were found. "
            "Inspect the pipeline output above and confirm the Gemini model can generate content."
        )

    print("\nExtracted entity relationships:")
    for record in records:
        print(
            f"  {record['source']} ({', '.join(record['source_labels'])}) "
            f"-[:{record['relationship']}]-> "
            f"{record['target']} ({', '.join(record['target_labels'])})"
        )

async def run_with_transient_retry(pipeline: SimpleKGPipeline, source: str):
    """Retry Gemini capacity errors without hiding configuration or extraction errors."""
    max_attempts = int(os.getenv("GEMINI_MAX_ATTEMPTS", "5"))
    if max_attempts < 1:
        raise ValueError("GEMINI_MAX_ATTEMPTS must be at least 1.")

    for attempt in range(1, max_attempts + 1):
        try:
            return await pipeline.run_async(
                text=source,
                file_path="controlled-python-auth-sample",
                document_metadata={
                    "source": "graphrag-kg-poc",
                    "kind": "controlled-code-sample",
                },
            )
        except LLMGenerationError as error:
            error_text = str(error).upper()
            is_transient_capacity_error = (
                "503" in error_text and "UNAVAILABLE" in error_text
            )
            if not is_transient_capacity_error or attempt == max_attempts:
                raise

            delay_seconds = 2 ** (attempt + 1)  # 4, 8, 16, 32 seconds
            print(
                f"Gemini is temporarily unavailable (attempt {attempt}/{max_attempts}). "
                f"Retrying in {delay_seconds} seconds..."
            )
            await asyncio.sleep(delay_seconds)

    raise AssertionError("Retry loop must either return a result or raise an error.")


async def main() -> None:
    api_key = required_env("GEMINI_API_KEY")
    uri = required_env("NEO4J_URI")
    username = required_env("NEO4J_USERNAME")
    password = required_env("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE") or None

    llm = GeminiLLM(
        model_name=os.getenv("GEMINI_LLM_MODEL", "gemini-3.8-flash"),
        api_key=api_key,
    )
    embedder = GeminiEmbedder(
        model=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dim=int(os.getenv("GEMINI_EMBEDDING_DIMENSIONS", "768")),
        api_key=api_key,
    )
    
    driver = CreateDriver(uri, username, password).get_driver()

    try:
        driver.verify_connectivity()
        pipeline = SimpleKGPipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            schema=SCHEMA,
            from_file=False,
            neo4j_database=database,
            on_error="RAISE",
        )
        await run_with_transient_retry(pipeline, load_source())
        
        # 2. Entity embeddings
        create_entity_embeddings(
            driver,
            database,
            embedder
        )

        # 3. Community detection
        ensure_projection(driver, database)

        stats = detect_communities(driver, database)
        print(
            f"Detected {stats['communityCount']} communities "
            f"across {stats['nodeCount']} entities"
        )

        communities = get_communities(driver, database)

        for community in communities:
            community_id = community["community_id"]
            entities = community["entities"]
            relationships = community["relationships"]

            summary = await generate_community_summary(
                llm,
                community_id,
                entities,
                relationships,
            )

            save_community(
                driver,
                database,
                community_id,
                entities,
                summary,
            )
        # community embeddings
        embed_communities(
            driver,
            database,
            embedder,
        )
        # inspect_graph(driver, database)
    finally:
        driver.close()
        await llm.aclose()

if __name__ == "__main__":
    asyncio.run(main())
