import json
import os
from dotenv import load_dotenv
from groq import Groq
load_dotenv()

from backend.ai_services.ingestion.discovery.read_write_plan import save_manifest, save_plan

from backend.ai_services.ingestion.discovery.planner import create_ingestion_plan
from backend.ai_services.ingestion.discovery.plan_validation import validate_ingestion_plan
from backend.ai_services.ingestion.discovery.repo import Repository
from backend.ai_services.ingestion.discovery.git_tree import get_head_commit, get_repository_files
from backend.ai_services.ingestion.discovery.repo_manifest import build_repository_manifest

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
repo = Repository.from_path(r"E:\Studies\Dev\Projects\claimIQ")

async def main():
    files = get_repository_files(repo.root)
    commit = get_head_commit(repo.root)

    manifest = build_repository_manifest(
        repo_name=repo.name,
        commit=commit,
        files=files,
    )
    manifest = manifest.to_json()
    save_manifest(manifest)

    client = Groq(
        api_key=GROQ_API_KEY,
    )

    architectural_objective = """
    Build an architectural knowledge graph for DecisionGuard.

    Prioritize production components, services, APIs, databases, data models,
    events, consumers, shared contracts, infrastructure, configuration, and
    documentation that explain how the system is structured and how components
    interact.

    The resulting graph will be used to identify architectural context for
    future pull requests.
    """

    plan = create_ingestion_plan(
        client=client,
        architectural_objective=architectural_objective,
        manifest=manifest,
    )
    validate_ingestion_plan(plan, json.loads(manifest))
    save_plan(plan)
    
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
