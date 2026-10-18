import json
import os
from dotenv import load_dotenv
from shared.ai.provider import create_provider
load_dotenv()

from ai_services.ingestion.discovery.read_write_plan import save_manifest, save_plan

from ai_services.ingestion.discovery.planner import create_vertex_ingestion_plan
from ai_services.ingestion.discovery.plan_validation import validate_ingestion_plan
from ai_services.ingestion.discovery.repo import Repository
from ai_services.ingestion.discovery.git_tree import get_head_commit, get_repository_files
from ai_services.ingestion.discovery.repo_manifest import build_repository_manifest

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

    services = create_provider()

    architectural_objective = """
    Build an architectural knowledge graph for DecisionGuard.

    Prioritize production components, services, APIs, databases, data models,
    events, consumers, shared contracts, infrastructure, configuration, and
    documentation that explain how the system is structured and how components
    interact.

    The resulting graph will be used to identify architectural context for
    future pull requests.
    """

    try:
        plan = await create_vertex_ingestion_plan(services.llm, architectural_objective, manifest)
    finally:
        await services.aclose()
    validate_ingestion_plan(plan, json.loads(manifest))
    save_plan(plan)
    
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
