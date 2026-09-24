import os
from dotenv import load_dotenv
load_dotenv()
from github_graphrag.ingestion.chunking.builder import build_chunks
from github_graphrag.ingestion.chunking.registry import register_chunkers
from github_graphrag.ingestion.planner import create_ingestion_plan
from github_graphrag.ingestion.repo import Repository
from github_graphrag.ingestion.git_tree import get_head_commit, get_repository_files
from github_graphrag.ingestion.repo_manifest import build_repository_manifest

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
repo = Repository.from_path(r"E:\Studies\Dev\Projects\claimIQ")

register_chunkers()

files = get_repository_files(repo.root)
commit = get_head_commit(repo.root)

manifest = build_repository_manifest(
    repo_name=repo.name,
    commit=commit,
    files=files,
)

from groq import Groq

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

manifest = manifest.to_json()

plan = create_ingestion_plan(
    client=client,
    architectural_objective=architectural_objective,
    manifest=manifest,
)

# for file_plan in plan.files:
#     print(
#         file_plan.path,
#         file_plan.action,
#         file_plan.chunk_strategy,
#         file_plan.reason,
#     )
    
chunks = build_chunks(
    repo_root=repo.root,
    plan=plan,
)
print(f"Built {len(chunks)} chunks for ingestion.")