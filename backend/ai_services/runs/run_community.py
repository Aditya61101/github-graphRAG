"""Run community detection, summarization, embedding, and vector indexing against Neo4j.

This script executes only the Community Layer directly on an existing graph in Neo4j.
It does not re-run repository ingestion, planning, or code chunking.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI

from ai_services.ingestion.community import (
    CommunityConfig,
    build_community_pipeline,
)
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.ingestion.rkg.llm_adapters import AzureOpenAILLM

load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}. Please check your .env file.")
    return value.strip("'\"")


def detect_entity_label(driver, database: str | None) -> str:
    """Auto-detect whether the graph uses '__Entity__' or 'Entity' node labels."""
    env_label = os.getenv("COMMUNITY_ENTITY_LABEL")
    if env_label:
        return env_label

    for label in ("__Entity__", "Entity"):
        try:
            records, _, _ = driver.execute_query(
                f"MATCH (e:{label}) RETURN count(e) AS cnt",
                database_=database,
            )
            if records and records[0]["cnt"] > 0:
                return label
        except Exception:
            continue

    # Default fallback
    return "Entity"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run community detection, summarization, and vector indexing on existing Neo4j graph."
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force regeneration of all community summaries and embeddings even if unchanged.",
    )
    parser.add_argument(
        "--algorithm",
        choices=["leiden", "louvain"],
        default="leiden",
        help="Community detection algorithm (default: leiden).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for community detection (default: 42).",
    )
    args = parser.parse_args()

    # 1. Neo4j connection configuration
    uri = _required_env("NEO4J_URI")
    username = _required_env("NEO4J_USERNAME")
    password = _required_env("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE") or None

    driver = GraphDatabase.driver(uri, auth=(username, password))

    # 2. Azure OpenAI configuration
    azure_endpoint = _required_env("AZURE_OPENAI_ENDPOINT")
    azure_api_key = _required_env("AZURE_OPENAI_API_KEY")
    azure_api_version = _required_env("AZURE_OPENAI_API_VERSION")
    llm_deployment = _required_env("AZURE_OPENAI_DEPLOYMENT_NAME")
    embedding_deployment = _required_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")
    embedding_dimensions = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072"))

    azure_client = AsyncAzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        api_version=azure_api_version,
    )

    llm = AzureOpenAILLM(
        client=azure_client,
        deployment=llm_deployment,
    )

    embedder = AzureOpenAIEmbedder(
        client=azure_client,
        deployment=embedding_deployment,
        dimensions=embedding_dimensions,
        batch_size=int(os.getenv("COMMUNITY_EMBEDDING_BATCH_SIZE", "32")),
    )

    try:
        driver.verify_connectivity()
        entity_label = detect_entity_label(driver, database)
        print(f"Connected to Neo4j at {uri}")
        print(f"Target database: {database or '(default)'}")
        print(f"Detected architectural entity label: :{entity_label}")
        print(f"Using algorithm: {args.algorithm} (seed: {args.seed})")
        print(f"LLM deployment: {llm_deployment}")
        print(f"Embedding deployment: {embedding_deployment} ({embedding_dimensions} dims)")
        print(f"Force refresh: {args.force_refresh}")
        print("\nStarting community processing...\n")

        config = CommunityConfig(
            graph_name="entityGraph",
            algorithm=args.algorithm,
            random_seed=args.seed,
            entity_label=entity_label,
            database=database,
            vector_index_name=os.getenv("COMMUNITY_VECTOR_INDEX", "community_vector_index"),
        )

        pipeline = build_community_pipeline(
            driver=driver,
            llm=llm,
            embedder=embedder,
            config=config,
        )

        result = await pipeline.run(force_refresh=args.force_refresh)

        print("=" * 60)
        print("           COMMUNITY LAYER PIPELINE RESULTS")
        print("=" * 60)
        print(f"Algorithm used:                 {result.algorithm}")
        print(f"Total communities processed:    {result.community_count}")
        print(f"Summaries newly generated:      {result.summaries_generated}")
        print(f"Summaries skipped (unchanged):  {result.summaries_skipped_unchanged}")
        print(f"Embeddings newly generated:     {result.embeddings_generated}")
        print("=" * 60)

    except Exception as exc:
        print(f"\n[ERROR] Community pipeline failed: {exc}", file=sys.stderr)
        raise
    finally:
        driver.close()


if __name__ == "__main__":
    asyncio.run(main())
