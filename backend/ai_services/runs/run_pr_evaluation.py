"""Shared Stage 4.3 worker process. No webhook, reranker or web server required.

python -m ai_services.runs.run_pr_evaluation --job-id JOB_ID
Cloud Run Jobs can supply PR_EVALUATION_JOB_ID instead.
"""

import argparse
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from neo4j import GraphDatabase

from ai_services.decision_guard.evaluation import UploadAuthority
from ai_services.decision_guard.graph import Neo4jArchitectureGraphReader
from ai_services.decision_guard.orchestration.runtime import (
    configured_evaluation_model,
    ProductionRuntime,
)
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.decision_guard.orchestration.worker import EvaluationWorker
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from shared.ai.provider import create_provider


async def run(job_id):
    from shared.utils.processing import require_background_processing
    require_background_processing()
    from api_services.app.config import ADRS_STORAGE_DIR

    store = SqliteApplicationStore()
    jobs = EvaluationStore(store.session_factory)
    if jobs.get(job_id) is None:
        raise ValueError("Unknown PR evaluation job ID")
    locks = {}
    @asynccontextmanager
    async def open_runtime(github):
        # Called only AFTER the durable worker claim and independent progress
        # publication task. Setup errors are recorded by the ordinary worker.
        driver = GraphDatabase.driver(
            os.environ['NEO4J_URI'],
            auth=(os.environ['NEO4J_USERNAME'], os.environ['NEO4J_PASSWORD']),
        )
        try:
            services = create_provider()
            try:
                llm = services.llm
                yield ProductionRuntime(
                    store,
                    github,
                    Neo4jArchitectureGraphReader(
                        driver, os.environ.get("NEO4J_DATABASE") or "neo4j"
                    ),
                    lambda key: locks.setdefault(key, asyncio.Lock()),
                    UploadAuthority(store, ADRS_STORAGE_DIR),
                    configured_evaluation_model(
                        llm,
                        deployment_revision=os.environ["PR_EVALUATION_DEPLOYMENT_REVISION"],
                    ),
                )
            finally:
                await services.aclose()
        finally:
            driver.close()
    async with httpx.AsyncClient(timeout=30) as http:
        github = GitHubAppService(store, GitHubAppSettings.from_env(), http)
        from ai_services.decision_guard.orchestration.checks import CheckPublisher
        publisher = CheckPublisher(
            jobs, github,
            enabled=os.getenv('GITHUB_CHECKS_ENABLED', 'false').lower() == 'true',
            details_base=os.getenv('GITHUB_CHECKS_DETAILS_BASE') or None,
        )
        await EvaluationWorker(jobs, None, publisher=publisher,
                               runtime_factory=lambda: open_runtime(github)).run_job(job_id)
        await publisher.reconcile()
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
