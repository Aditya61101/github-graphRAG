"""Shared Stage 4.3 worker process. No webhook, reranker or web server required.

python -m ai_services.runs.run_pr_evaluation --job-id JOB_ID
Cloud Run Jobs can supply PR_EVALUATION_JOB_ID instead.
"""

import argparse
import asyncio
import os

import httpx
from neo4j import GraphDatabase
from openai import AsyncAzureOpenAI

from ai_services.decision_guard.evaluation import UploadAuthority
from ai_services.decision_guard.graph import Neo4jArchitectureGraphReader
from ai_services.decision_guard.orchestration.runtime import (
    ConfiguredAzureModel,
    ProductionRuntime,
)
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.decision_guard.orchestration.worker import EvaluationWorker
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from shared.utils.llm_utils import AzureOpenAILLM


async def run(job_id):
    from api_services.app.config import ADRS_STORAGE_DIR

    store = SqliteApplicationStore()
    jobs = EvaluationStore(store.session_factory)
    if jobs.get(job_id) is None:
        raise ValueError("Unknown PR evaluation job ID")
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    locks = {}
    async with (
        httpx.AsyncClient(timeout=30) as http,
        AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ["AZURE_OPENAI_API_VERSION"],
            max_retries=0,
        ) as client,
    ):
        try:
            github = GitHubAppService(store, GitHubAppSettings.from_env(), http)
            llm = AzureOpenAILLM(client, os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"])
            runtime = ProductionRuntime(
                store,
                github,
                Neo4jArchitectureGraphReader(
                    driver, os.environ.get("NEO4J_DATABASE") or "neo4j"
                ),
                lambda key: locks.setdefault(key, asyncio.Lock()),
                UploadAuthority(store, ADRS_STORAGE_DIR),
                ConfiguredAzureModel(
                    llm,
                    deployment_revision=os.environ["PR_EVALUATION_DEPLOYMENT_REVISION"],
                ),
            )
            await EvaluationWorker(jobs, runtime).run_job(job_id)
        finally:
            driver.close()
    job = jobs.get(job_id)
    print(f"job={job.id} status={job.status} stage={job.stage}")
    return 1 if job.status in {"FAILED", "RETRY", "RUNNING"} else 0


def main():
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default=os.getenv("PR_EVALUATION_JOB_ID"))
    args = parser.parse_args()
    if not args.job_id:
        parser.error("--job-id or PR_EVALUATION_JOB_ID is required")
    raise SystemExit(asyncio.run(run(args.job_id)))


if __name__ == "__main__":
    main()
