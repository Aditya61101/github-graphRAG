"""Stage 4.4 offline tests. Real Case A is read, never overwritten or reevaluated."""

# ruff: noqa: F811 -- pytest injects the imported shared env fixture by name.

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.decision_guard.orchestration.checks import (
    CheckPublisher,
    check_payload,
    conclusion,
)
from ai_services.decision_guard.orchestration.projections import (
    RepositoryReadModel,
    backfill,
    request_sync,
    sync_pr,
    synchronize,
    synchronize_due,
)
from ai_services.decision_guard.orchestration.read_models import (
    TABLES,
    CheckPublication,
    EvaluationProjection,
    FindingRule,
    PullRequestState,
    PullRequestSyncIntent,
)
from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.migrate_frontend_checks import upgrade
from ai_services.tests.test_pr_ingestion import payload
from ai_services.tests.test_pr_orchestration import (
    deliver,
    env,  # noqa: F401 -- shared offline fixture
)
from api_services.app.routers.repositories import router as repository_router


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404])
async def test_details_link_only_published_when_configured_and_accessible(
    env, github, status
):
    job_id = await evaluated(env)
    github.app.client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=status))
    )
    await CheckPublisher(
        env.store,
        github.app,
        enabled=True,
        details_base="https://decisionguard.example",
    ).publish(job_id)
    assert ("details_url" in github.writes[0][2]) == (status == 200)
    assert github.app.client.get.call_args.kwargs == {
        "timeout": 5,
        "follow_redirects": False,
    }


@pytest.mark.asyncio
async def test_operational_failure_publishes_without_result_or_model(env, github):
    job_id = env.store.enqueue("repo_101", env.event, env.settings)
    token = env.store.claim(job_id)
    env.store.fail(job_id, token, "SOURCE_UNAVAILABLE", retryable=False)
    await CheckPublisher(env.store, github.app, enabled=True).publish(job_id)
    assert github.writes[0][2]["conclusion"] == "action_required"
    assert env.store.get(job_id).status == "FAILED" and not env.model.requests


def metadata(
    env, *, state="open", merged=False, updated="2026-10-16T01:00:00Z", head=None
):
    return {
        "number": env.event.pull_request_number,
        "title": "Regression PR",
        "user": {"login": "author"},
        "state": state,
        "merged": merged,
        "updated_at": updated,
        "base": {
            "ref": "main",
            "sha": env.event.base_sha,
            "repo": {"id": int(env.event.github_repository_id)},
        },
        "head": {"ref": "feature", "sha": head or env.event.head_sha},
    }


async def evaluated(env):
    job_id = env.store.enqueue("repo_101", env.event, env.settings)
    await env.worker.run_job(job_id)
    return job_id


@pytest.fixture
def github(env):
    runs = []
    pr = metadata(env)
    writes = []

    async def request(method, path, token, **kwargs):
        if method == "GET":
            if "/check-runs/" in path:
                return next(r for r in runs if str(r["id"]) == path.rsplit("/", 1)[1])
            return pr
        writes.append((method, path, kwargs["json"]))
        data = kwargs["json"]
        response = {
            "id": 100,
            "head_sha": env.event.head_sha,
            "app": {"id": 42},
            "url": "https://api.github.com/repos/owner/repo/check-runs/100",
            **data,
        }
        if method == "POST":
            runs.append(response)
        return response

    app = SimpleNamespace(
        store=env.application_store,
        settings=SimpleNamespace(app_id="42"),
        indexing_credential=AsyncMock(),
        installation_token=AsyncMock(return_value=SimpleNamespace(token="offline")),
        request=AsyncMock(side_effect=request),
        pages=AsyncMock(side_effect=lambda *a, **kw: runs.copy()),
    )
    return SimpleNamespace(app=app, runs=runs, pr=pr, writes=writes)


@pytest.mark.asyncio
async def test_projection_transaction_and_idempotent_backfill(env):
    job_id = await evaluated(env)
    before = env.store.get(job_id).result_json
    backfill(env.store)
    backfill(env.store)
    with env.sessions() as s:
        assert len(list(s.scalars(select(EvaluationProjection)))) == 1
        assert len(list(s.scalars(select(CheckPublication)))) == 1
        result = EvaluationResult.model_validate_json(before)
        assert len(list(s.scalars(select(FindingRule)))) == len(result.findings)
    assert env.store.get(job_id).result_json == before


@pytest.mark.asyncio
async def test_one_pr_history_freshness_lifecycle_and_distinct_analysis_count(env):
    job_id = await evaluated(env)
    read = RepositoryReadModel(env.store)
    repo = env.store.get(job_id).repository_id
    assert read.list_prs(repo)["items"][0]["state"] == "unknown"
    sync_pr(env.store, repo, metadata(env), observed_at=1000)
    assert read.list_prs(repo)["items"][0]["freshness"] == "CURRENT_OBSERVED"
    for state, merged in [("closed", False), ("closed", True)]:
        sync_pr(
            env.store,
            repo,
            metadata(
                env,
                state=state,
                merged=merged,
                updated="2026-10-16T02:00:00Z"
                if not merged
                else "2026-10-16T03:00:00Z",
            ),
            observed_at=1100 if not merged else 1200,
        )
        assert read.overview(repo)["architectural_warnings"] == 0
        assert read.overview(repo)["prs_analyzed"] == 1
    assert not sync_pr(env.store, repo, metadata(env), observed_at=1300)
    sync_pr(
        env.store, repo, metadata(env, updated="2026-10-16T04:00:00Z"), observed_at=1400
    )
    assert read.list_prs(repo)["items"][0]["state"] == "open"
    newer = env.event.model_copy(update={"head_sha": "d" * 40, "delivery_id": "new"})
    env.store.enqueue(repo, newer)
    sync_pr(
        env.store,
        repo,
        metadata(env, updated="2026-10-16T05:00:00Z", head="d" * 40),
        observed_at=1500,
    )
    page = read.list_prs(repo)
    assert page["total"] == len(page["items"]) == 1
    assert page["items"][0]["evaluation"]["outcome"] is None
    assert read.overview(repo)["architectural_warnings"] == 0
    detail = read.detail(repo, env.event.pull_request_number, limit=1)
    assert detail["history_total"] == 2 and len(detail["history"]) == 1
    assert read.overview(repo)["prs_analyzed"] == 1


@pytest.mark.asyncio
async def test_risk_identity_dedup_classification_and_isolation(env):
    job_id = await evaluated(env)
    repo = env.store.get(job_id).repository_id
    sync_pr(env.store, repo, metadata(env), observed_at=1000)
    with env.sessions.begin() as s:
        s.query(FindingRule).delete()
        for fid, decision, classification in [
            ("a", "decision1", "CONFIRMED"),
            ("b", "decision1", "CONFIRMED"),
            ("c", "decision1", "POTENTIAL"),
            ("d", "decision2", "POTENTIAL"),
            ("e", None, "CONFIRMED"),
        ]:
            s.add(
                FindingRule(
                    job_id=job_id,
                    finding_id=fid,
                    rule_id=fid,
                    decision_id=decision,
                    requirement_kind="DECISION" if decision else "CONSTRAINT",
                    adr_ids_json='["adr1"]',
                    classification=classification,
                )
            )
    result = RepositoryReadModel(env.store).overview(repo)
    assert result["decisions_at_risk"] == 2
    assert (
        result["confirmed_decisions"]
        == result["potential_decisions"]
        == result["unmapped_requirements"]
        == 1
    )
    assert RepositoryReadModel(env.store).overview("other")["decisions_at_risk"] == 0


def test_migration_portability_idempotency_and_preservation(env):
    with env.engine.begin() as c:
        upgrade(c)
        upgrade(c)
        assert (
            c.execute(
                text("SELECT version FROM dg_frontend_checks_schema_version")
            ).scalar()
            == 1
        )
        assert all(t.name in inspect(c).get_table_names() for t in TABLES)
        assert c.execute(text("SELECT COUNT(*) FROM repositories")).scalar() == 1
    for table in TABLES:
        assert "CREATE TABLE" in str(
            CreateTable(table).compile(dialect=postgresql.dialect())
        )


@pytest.mark.asyncio
async def test_check_idempotent_concurrent_create_update_and_no_model_calls(
    env, github
):
    job_id = await evaluated(env)
    attempts = env.store.get(job_id).provider_attempts
    publisher = CheckPublisher(env.store, github.app, enabled=True)
    await asyncio.gather(*(publisher.publish(job_id) for _ in range(5)))
    assert [w[0] for w in github.writes] == ["POST"]
    publisher.retry(job_id)
    await publisher.reconcile()
    assert [w[0] for w in github.writes] == ["POST", "PATCH"]
    with env.sessions() as s:
        row = s.get(CheckPublication, job_id)
        assert row.status == "PUBLISHED" and row.check_run_id == "100"
    assert env.store.get(job_id).provider_attempts == attempts


@pytest.mark.asyncio
async def test_concurrent_publication_claims(env):
    job_id = await evaluated(env)
    publisher = CheckPublisher(env.store, None, enabled=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: publisher.claim(job_id), range(8)))
    assert sum(c is not None for c in claims) == 1


@pytest.mark.asyncio
async def test_transient_failure_retry_survives_restart_without_evaluation(env, github):
    job_id = await evaluated(env)
    original = github.app.request.side_effect
    github.app.request.side_effect = GitHubAppError(503, "offline")
    publisher = CheckPublisher(env.store, github.app, enabled=True)
    await publisher.publish(job_id)
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "RETRY"
    assert env.store.get(job_id).result_json
    github.app.request.side_effect = original
    env.clock.now += 10
    await CheckPublisher(env.store, github.app, enabled=True).reconcile()
    assert len(github.writes) == 1


@pytest.mark.asyncio
async def test_lost_create_response_is_reconciled_never_blindly_recreated(env, github):
    job_id = await evaluated(env)
    original = github.app.request.side_effect

    async def lost(method, *args, **kwargs):
        response = await original(method, *args, **kwargs)
        if method == "POST":
            raise GitHubAppError(502, "response lost")
        return response

    github.app.request.side_effect = lost
    publisher = CheckPublisher(env.store, github.app, enabled=True)
    await publisher.publish(job_id)
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "CREATE_UNKNOWN"
    env.clock.now += 10
    github.app.request.side_effect = original
    await publisher.reconcile()
    assert [w[0] for w in github.writes] == ["POST", "PATCH"]
    assert len(github.runs) == 1


@pytest.mark.asyncio
async def test_ambiguous_create_absent_from_list_does_not_duplicate(env, github):
    job_id = await evaluated(env)
    with env.sessions.begin() as s:
        row = s.get(CheckPublication, job_id)
        row.create_started_at = 900
        row.status = "CREATE_UNKNOWN"
    await CheckPublisher(env.store, github.app, enabled=True).reconcile()
    assert not github.writes
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "CREATE_UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,head", [("closed", None), ("open", "d" * 40)])
async def test_stale_or_closed_never_publishes(env, github, state, head):
    job_id = await evaluated(env)
    github.pr.update(metadata(env, state=state, head=head))
    await CheckPublisher(env.store, github.app, enabled=True).publish(job_id)
    assert not github.writes
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "STALE"


@pytest.mark.asyncio
async def test_disabled_publisher_makes_no_github_request(env, github):
    job_id = await evaluated(env)
    await CheckPublisher(env.store, github.app).publish(job_id)
    github.app.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_apis_authorization_history_and_removed_routes(env):
    job_id = await evaluated(env)
    repo = env.store.get(job_id).repository_id
    env.app.include_router(repository_router, prefix="/repositories")
    with TestClient(env.app) as client:
        base = f"/repositories/{repo}"
        assert client.get(base + "/pull-requests").status_code == 401
        listing = client.get(base + "/pull-requests", headers=env.headers)
        assert listing.status_code == 200 and listing.json()["total"] == 1
        detail = client.get(
            base + f"/pull-requests/{env.event.pull_request_number}",
            headers=env.headers,
        )
        assert detail.status_code == 200
        assert detail.json()["result"] == json.loads(env.store.get(job_id).result_json)
        assert (
            client.get(
                base + "/pull-requests?limit=101", headers=env.headers
            ).status_code
            == 422
        )
        for suffix in ("findings", "coverage", "diagnostics"):
            assert (
                client.get(
                    base + f"/evaluations/{job_id}/{suffix}", headers=env.headers
                ).status_code
                == 404
            )
        overview = client.get(base + "/overview", headers=env.headers)
        assert overview.status_code == 200
        assert overview.json()["kpis"]["prs_analyzed"]["value"] == 1


def test_case_a_real_result_mapping_output_and_limits():
    folder = Path(__file__).resolve().parents[2] / "data" / "stage_4_3_live"
    if not (folder / "approved_full_run_result.json").exists():
        pytest.skip("Local real Case A evidence is unavailable")
    result = EvaluationResult.model_validate_json(
        (folder / "approved_full_run_result.json").read_text(encoding="utf-8-sig")
    )
    job = SimpleNamespace(
        id="case-a",
        head_sha=result.revision.head_sha,
        repository_id=result.revision.repository_id,
        pr_number=2,
    )
    output = check_payload(job, result, "Aditya61101/claimIQ")
    assert output["conclusion"] == "failure"
    summary = output["output"]["summary"]
    for value in (
        "Architectural conflicts detected",
        "**Supported governing-rule findings:** 3",
        "**Additional findings requiring review:** 1",
        "**Coverage:** Partial",
        "6 uncertain",
        "The independent review could not validate the required original-source evidence.",
        "uploaded source; no public link",
    ):
        assert value in summary
    assert "https://github.com/Aditya61101/claimIQ/blob/" in summary
    assert len(summary.encode()) < 65535 and "annotations" not in output["output"]
    assert "details_url" not in output
    assert conclusion(None) == "action_required"
    # Mapping unit test copies are never persisted and never masquerade as evidence.
    clean = result.model_copy(
        update={
            "findings": (),
            "rejected_findings": (),
            "limitations": (),
            "outcome": "NO_CONFLICTS_FOUND",
            "coverage": result.coverage.model_copy(
                update={"scope": "COMPLETE_FOR_RETAINED_SCOPE", "uncertain": 0}
            ),
        }
    )
    assert conclusion(clean) == "success"
    assert (
        conclusion(clean.model_copy(update={"limitations": result.limitations}))
        == "neutral"
    )
    assert (
        conclusion(
            result.model_copy(update={"findings": (), "outcome": "REVIEW_REQUIRED"})
        )
        == "neutral"
    )
    assert (
        conclusion(result.model_copy(update={"outcome": "NOT_EVALUATED"}))
        == "action_required"
    )
    with pytest.raises(ValueError):
        check_payload(job, result, "owner/repo", details_base="http://localhost:5173")


@pytest.mark.asyncio
async def test_metadata_failure_marks_unknown_and_cannot_overwrite_newer_observation(
    env, github
):
    job_id = await evaluated(env)
    repo = env.store.get(job_id).repository_id
    await synchronize(env.store, github.app, repo, env.event.pull_request_number)
    github.app.request.side_effect = GitHubAppError(503, "offline")
    with pytest.raises(GitHubAppError):
        await synchronize(env.store, github.app, repo, env.event.pull_request_number)
    assert RepositoryReadModel(env.store).overview(repo)["architectural_warnings"] == 0
    with env.sessions() as s:
        assert (
            s.get(PullRequestState, (repo, env.event.pull_request_number)).freshness
            == "UNKNOWN"
        )
    assert not sync_pr(
        env.store,
        repo,
        metadata(env, updated="2026-10-16T02:00:00Z"),
        observed_at=env.clock.now - 1,
    )


@pytest.mark.asyncio
async def test_equal_timestamp_contradiction_is_unknown_then_newer_snapshot_resolves(
    env,
):
    job_id = await evaluated(env)
    repo = env.store.get(job_id).repository_id
    sync_pr(env.store, repo, metadata(env), observed_at=1000)
    assert not sync_pr(env.store, repo, metadata(env, state="closed"), observed_at=1001)
    assert RepositoryReadModel(env.store).overview(repo)["architectural_warnings"] == 0
    assert sync_pr(
        env.store,
        repo,
        metadata(env, state="closed", updated="2026-10-16T02:00:00Z"),
        observed_at=1002,
    )
    assert (
        RepositoryReadModel(env.store).list_prs(repo)["items"][0]["state"] == "closed"
    )


@pytest.mark.parametrize("merged", [False, True])
def test_signed_closed_duplicates_only_synchronize_lifecycle(env, merged):
    import hashlib
    import hmac

    repo = "repo_101"
    remote = metadata(env, state="closed", merged=merged)
    env.app.state.github_app.request.return_value = remote
    data = payload(env.event)
    data["action"] = "closed"
    data["pull_request"]["merged"] = merged
    raw = json.dumps(data).encode()
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    with TestClient(env.app) as client:
        for _ in range(2):
            response = client.post(
                "/webhooks/github",
                content=raw,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "close-1",
                    "X-Hub-Signature-256": "sha256=" + signature,
                },
            )
            assert response.status_code == 200
        # A late opened webhook re-fetches CLOSED; cannot reopen persisted state.
        env.runtime.event = None
        response = deliver(client, env.event)
        assert response.status_code == 202
    assert RepositoryReadModel(env.store).list_prs(repo)["items"][0]["state"] == (
        "merged" if merged else "closed"
    )
    assert not env.model.requests


@pytest.mark.asyncio
async def test_expired_publication_recovery_and_foreign_app_run_not_adopted(
    env, github
):
    job_id = await evaluated(env)
    github.runs.append(
        {
            "id": 999,
            "head_sha": env.event.head_sha,
            "external_id": f"decisionguard:{job_id}",
            "app": {"id": 999},
        }
    )
    pub = CheckPublisher(env.store, github.app, enabled=True)
    assert pub.claim(job_id)
    env.clock.now += 181
    await CheckPublisher(env.store, github.app, enabled=True).reconcile()
    assert len(github.writes) == 1 and github.writes[0][0] == "POST"


@pytest.mark.asyncio
async def test_permission_failure_retry_does_not_require_another_result(env, github):
    job_id = await evaluated(env)
    original = github.app.request.side_effect

    async def denied(method, *args, **kwargs):
        if method == "POST":
            raise GitHubAppError(403, "denied")
        return await original(method, *args, **kwargs)

    pub = CheckPublisher(env.store, github.app, enabled=True)
    github.app.request.side_effect = denied
    await pub.publish(job_id)
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "BLOCKED"
        assert s.get(CheckPublication, job_id).create_started_at is None
    saved = env.store.get(job_id).result_json
    github.app.request.side_effect = original
    pub.retry(job_id)
    await pub.publish(job_id)
    assert env.store.get(job_id).result_json == saved
    assert len(github.writes) == 1


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_with_result_finalization(env, monkeypatch):
    from ai_services.decision_guard.orchestration import projections
    from ai_services.decision_guard.orchestration.models import EvaluationJob

    job_id = await evaluated(env)
    with env.sessions.begin() as s:
        job = s.get(EvaluationJob, job_id)
        saved = job.result_json
        job.status, job.lease_token, job.lease_until = (
            "RUNNING",
            "owned",
            env.clock.now + 30,
        )
        job.result_json = None
        s.query(EvaluationProjection).delete()
        s.query(CheckPublication).delete()

    def fail(*args):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(projections, "project", fail)
    with pytest.raises(RuntimeError):
        env.store.finish(job_id, "owned", EvaluationResult.model_validate_json(saved))
    assert env.store.get(job_id).result_json is None
    assert env.store.get(job_id).status == "RUNNING"
    with env.sessions() as s:
        assert not list(s.scalars(select(CheckPublication)))


@pytest.mark.asyncio
async def test_metadata_outbox_recovers_closed_pr_without_analysis(env, github):
    request_sync(env.store, "repo_101", env.event.pull_request_number)
    github.app.request.side_effect = GitHubAppError(503, "temporary outage")
    await synchronize_due(env.store, github.app)
    with env.sessions() as s:
        intent = s.get(
            PullRequestSyncIntent, ("repo_101", env.event.pull_request_number)
        )
        assert (
            intent.attempts == 1 and intent.error_code == "GITHUB_METADATA_UNAVAILABLE"
        )
    github.app.request.side_effect = None
    github.app.request.return_value = metadata(env, state="closed", merged=True)
    env.clock.now += 5
    await synchronize_due(env.store, github.app)
    with env.sessions() as s:
        assert (
            s.get(PullRequestSyncIntent, ("repo_101", env.event.pull_request_number))
            is None
        )
        assert (
            s.get(PullRequestState, ("repo_101", env.event.pull_request_number)).state
            == "merged"
        )
    assert env.store.list("repo_101") == [] and not env.model.requests


@pytest.mark.asyncio
async def test_reopened_superseded_revision_reuses_result_without_provider_calls(
    env, github
):
    from ai_services.decision_guard.orchestration.models import EvaluationJob

    job_id = await evaluated(env)
    before = env.store.get(job_id).provider_attempts
    with env.sessions.begin() as s:
        s.get(EvaluationJob, job_id).status = "SUPERSEDED"
        s.get(CheckPublication, job_id).status = "STALE"
    sync_pr(
        env.store,
        "repo_101",
        metadata(env, updated="2026-10-16T03:00:00Z"),
        observed_at=1200,
    )
    reopened = env.event.model_copy(
        update={"source_event": "reopened", "delivery_id": "reopen-1"}
    )
    assert env.store.enqueue("repo_101", reopened, env.settings) == job_id
    await env.worker.run_job(job_id)
    assert env.store.get(job_id).status == "COMPLETED"
    assert env.store.get(job_id).provider_attempts == before
    with env.sessions() as s:
        assert s.get(CheckPublication, job_id).status == "PENDING"
