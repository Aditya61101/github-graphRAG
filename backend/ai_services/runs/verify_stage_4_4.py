"""Verify real stored data and preview a Check, without constructing an Azure client.

Default performs GitHub reads, local migration/projection backfill and API reads.
--publish is a separate operator action requiring explicit GitHub write approval.
Run from backend: python -m ai_services.runs.verify_stage_4_4 --job-id JOB
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from neo4j import GraphDatabase
from sqlalchemy import select

from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.decision_guard.orchestration.checks import (
    CheckPublisher,
    check_payload,
)
from ai_services.decision_guard.orchestration.models import EvaluationJob, ModelPacket
from ai_services.decision_guard.orchestration.projections import (
    backfill,
    publication_data,
    synchronize,
)
from ai_services.decision_guard.orchestration.read_models import (
    CheckPublication,
    FindingRule,
)
from ai_services.decision_guard.orchestration.store import EvaluationStore
from ai_services.github_app.service import GitHubAppService
from ai_services.github_app.settings import GitHubAppSettings
from ai_services.graph.repository import Neo4jGraphRepository
from ai_services.graph.service import RepositoryGraphService
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore


def fingerprint(store, job_id):
    with store.sessions() as session:
        job = session.get(EvaluationJob, job_id)
        return {
            "result_sha256": hashlib.sha256(job.result_json.encode()).hexdigest(),
            "context_sha256": hashlib.sha256(
                (job.context_json or "").encode()
            ).hexdigest(),
            "changes_sha256": hashlib.sha256(
                (job.changes_json or "").encode()
            ).hexdigest(),
            "provider_attempts": job.provider_attempts,
            "charged_tokens": job.charged_tokens,
            "charged_cost": job.charged_cost,
            "worker_attempts": job.attempts,
            "settings_sha256": hashlib.sha256(job.settings_json.encode()).hexdigest(),
            "packets": [
                {
                    "key": p.packet_key,
                    "state": p.state,
                    "reply_hash": hashlib.sha256(
                        (p.reply_json or "").encode()
                    ).hexdigest(),
                }
                for p in session.scalars(
                    select(ModelPacket)
                    .where(ModelPacket.job_id == job_id)
                    .order_by(ModelPacket.packet_key)
                )
            ],
        }


async def run(args):
    # Importing routers/config does not initialize the main application lifespan.
    from api_services.app.routers.evaluations import router as evaluation_router
    from api_services.app.routers.repositories import router as repository_router
    from api_services.app.utils.jwt_utils import create_access_token

    application = SqliteApplicationStore()
    store = EvaluationStore(application.session_factory)
    job = store.get(args.job_id)
    if job is None or not job.result_json:
        raise ValueError("A persisted evaluated job is required")
    before = fingerprint(store, job.id)
    result = EvaluationResult.model_validate_json(job.result_json)
    repo = application.get_repository(job.repository_id)
    backfill(store)
    folder = Path("data/stage_4_4")
    folder.mkdir(parents=True, exist_ok=True)
    report = {
        "job_id": job.id,
        "repository": repo.full_name,
        "pr_number": job.pr_number,
        "head_sha": job.head_sha,
        "new_model_calls": 0,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        github = GitHubAppService(application, GitHubAppSettings.from_env(), client)
        await github.authorize_tracked(repo.user_id, repo.id, require_admin=True)
        await synchronize(store, github, repo.id, job.pr_number)
        installation = await github.request(
            "GET", f"/repos/{repo.full_name}/installation", github.app_jwt()
        )
        report["installation_id"] = installation.get("id")
        report["installation_permissions"] = installation.get("permissions", {})
        with store.sessions() as session:
            rules = list(
                session.scalars(select(FindingRule).where(FindingRule.job_id == job.id))
            )
        preview = check_payload(
            job,
            result,
            repo.full_name,
            details_base=os.getenv("GITHUB_CHECKS_DETAILS_BASE") or None,
            rules=rules,
        )
        (folder / "check_preview.json").write_text(
            json.dumps(preview, indent=2), encoding="utf-8"
        )

        driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        )
        try:
            app = FastAPI()
            app.state.sqlite_store, app.state.github_app = application, github
            app.state.evaluation_orchestrator = SimpleNamespace(store=store)
            app.state.graph_service = RepositoryGraphService(
                application,
                Neo4jGraphRepository(
                    driver, database=os.getenv("NEO4J_DATABASE") or "neo4j"
                ),
                read_authorizer=github.authorize_repository_read,
            )
            app.include_router(evaluation_router, prefix="/repositories")
            app.include_router(repository_router, prefix="/repositories")
            headers = {
                "Authorization": "Bearer " + create_access_token({"sub": repo.user_id})
            }
            paths = {
                "overview": f"/repositories/{repo.id}/overview",
                "pull_requests": f"/repositories/{repo.id}/pull-requests",
                "detail": f"/repositories/{repo.id}/pull-requests/{job.pr_number}",
                "result": f"/repositories/{repo.id}/evaluations/{job.id}/result",
            }
            # These are the actual production routers, auth dependencies and real
            # DB/graph/GitHub, hosted in-process without starting/stopping services.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://verification"
            ) as api:
                report["production_router_statuses"] = {}
                responses = {}
                for name, path in paths.items():
                    response = await api.get(path, headers=headers)
                    report["production_router_statuses"][name] = response.status_code
                    response.raise_for_status()
                    responses[name] = response.json()
                assert responses["result"] == result.model_dump(mode="json")
                assert responses["detail"]["result"] == responses["result"]
                assert any(
                    p["pr_number"] == job.pr_number
                    for p in responses["pull_requests"]["items"]
                )
                report["overview"] = responses["overview"]
                report["pull_requests"] = responses["pull_requests"]
                report["detail_matches_canonical_result"] = True
                report["governing_rules"] = responses["detail"]["governing_rules"]
            # Independently inspect the running server if it is available. Do not
            # start, stop or restart it if it is absent or has older source loaded.
            try:
                schema = await client.get(
                    "http://127.0.0.1:8000/openapi.json", timeout=5
                )
                report["running_server_contract_loaded"] = paths[
                    "pull_requests"
                ].replace(repo.id, "{repository_id}") in schema.json().get("paths", {})
                report["running_server_statuses"] = {}
                for name, path in paths.items():
                    response = await client.get(
                        "http://127.0.0.1:8000" + path, headers=headers
                    )
                    report["running_server_statuses"][name] = response.status_code
            except (httpx.HTTPError, ValueError):
                report["running_server_available"] = False
        finally:
            driver.close()
        if args.publish:
            publisher = CheckPublisher(
                store,
                github,
                enabled=True,
                details_base=os.getenv("GITHUB_CHECKS_DETAILS_BASE") or None,
                required_check_id=getattr(args, "existing_check_id", None),
            )
            with store.sessions() as session:
                row = session.get(CheckPublication, job.id)
                status = row.status
            if status not in {"PENDING", "RETRY", "CREATE_UNKNOWN"}:
                publisher.retry(job.id)
            await publisher.publish(job.id)
        with store.sessions() as session:
            report["publication"] = publication_data(
                session.get(CheckPublication, job.id)
            )
    after = fingerprint(store, job.id)
    report["canonical_evidence_and_accounting_unchanged"] = before == after
    assert before == after
    report["preserved_fingerprint"] = after
    (folder / "verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "repository",
                    "pr_number",
                    "head_sha",
                    "production_router_statuses",
                    "installation_permissions",
                    "publication",
                    "canonical_evidence_and_accounting_unchanged",
                )
            },
            indent=2,
        )
    )


def main():
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--existing-check-id",
        help="Require this persisted Check ID; prohibit creating a replacement",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Requires separate explicit permission for a real GitHub Check write",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
