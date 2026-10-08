

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from neo4j import GraphDatabase

from backend.ai_services.ingestion.discovery.git_tree import get_head_commit
from ai_services.ingestion.rkg.builders.repo_ingestion_pipeline import build_repository_ingestion_pipeline
from backend.ai_services.ingestion.discovery.read_write_plan import load_plan
from ai_services.runs.run_ingestion import repo

ingestion_plan = load_plan()

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required. Copy .env.example and set its value.")
    return value

async def main():
    neo4j_driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(
            os.environ["NEO4J_USERNAME"],
            os.environ["NEO4J_PASSWORD"],
        ),
    )
    
    pipeline = build_repository_ingestion_pipeline(
        repository_root=repo.root,
        repository_id=repo.name,
        repository_name=repo.name,
        commit=get_head_commit(repo.root),
        neo4j_driver=neo4j_driver,
        candidates_path=".state/candidates.jsonl",
        examples=''
    )
    await pipeline.ingest(
        ingestion_plan.files
    )
    # print("ingestion result:", result)
if __name__ == "__main__":
    asyncio.run(main())
