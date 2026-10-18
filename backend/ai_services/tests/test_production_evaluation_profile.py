"""Offline production admission, packet feasibility and continuation regressions.

Case A assertions use its saved audit, not a reconstruction of PR #3.
"""

# ruff: noqa: F811 -- shared pytest fixtures are injected by name.
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tiktoken
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.context import conservative_tokens
from ai_services.decision_guard.evaluation.engine import (
    RequestBudget,
    build_units,
    prepare_evaluation_evidence,
)
from ai_services.decision_guard.evaluation.model_adapter import wire_text
from ai_services.decision_guard.evaluation import DecisionIntegrityEvaluator
from ai_services.decision_guard.evaluation.models import EvaluationResult, EvaluationSettings
from ai_services.decision_guard.orchestration.checks import CheckPublisher
from ai_services.decision_guard.orchestration.dispatch import EvaluationOrchestrator
from ai_services.decision_guard.orchestration.models import ModelPacket
from ai_services.decision_guard.orchestration.profiles import (
    AZURE_PRODUCTION_PROFILE,
    EvaluationConfigurationError,
    automatic_evaluation_settings,
    azure_production_settings,
)
from ai_services.decision_guard.orchestration.read_models import CheckPublication
from ai_services.tests.test_decision_guard_context import REPO
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    fixture,
)
from ai_services.tests.test_frontend_checks import github  # noqa: F401
from ai_services.tests.test_pr_orchestration import deliver, env  # noqa: F401
from ai_services.tests.test_evaluation_proof_refinement import store_policy
from ai_services.decision_guard.orchestration.runtime import ProductionPipeline, ProductionRuntime


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    original_connect = socket.socket.connect

    def blocked(sock, address):
        # Windows asyncio implements its wakeup socketpair over loopback TCP.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("Production profile tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def production_env(**updates):
    return {
        "K_SERVICE": "decisionguard-api",
        "PR_EVALUATION_SETTINGS": json.dumps(AZURE_PRODUCTION_PROFILE),
        **updates,
    }


@pytest.mark.parametrize("marker", ["K_SERVICE", "APP_ENV"])
@pytest.mark.parametrize("raw", [None, "", "{}", "null", "[]", "invalid", '{"token_encoding":null}'])
def test_production_missing_or_invalid_profile_fails_explicitly(marker, raw):
    values = {marker: "production"}
    if raw is not None:
        values["PR_EVALUATION_SETTINGS"] = raw
    with pytest.raises(EvaluationConfigurationError, match="PR_EVALUATION_SETTINGS"):
        automatic_evaluation_settings(values)


@pytest.mark.parametrize("field,bad", [
    ("token_encoding", None), ("max_input_tokens", 16000),
    ("max_output_tokens", 4000), ("provider_reserve", 512),
    ("max_tokens", 60000), ("max_attempts", 8),
    ("deadline_seconds", 4200.0), ("concurrency", 1),
    ("model_window", 16000), ("profile", "EXPANDED"),
])
def test_production_rejects_old_or_incompatible_profile(field, bad):
    raw = json.dumps({**AZURE_PRODUCTION_PROFILE, field: bad})
    with pytest.raises(EvaluationConfigurationError, match=field):
        automatic_evaluation_settings(production_env(PR_EVALUATION_SETTINGS=raw))


def test_development_defaults_and_unrelated_production_overrides_are_preserved():
    assert automatic_evaluation_settings({}) == EvaluationSettings()
    dev = automatic_evaluation_settings({"PR_EVALUATION_SETTINGS": '{"max_tokens":17}'})
    assert dev.max_tokens == 17 and dev.token_encoding is None
    overrides = {
        **AZURE_PRODUCTION_PROFILE, "max_reads": 5, "max_source_bytes": 10000,
        "retries": 0, "input_rate_per_million": 1.0,
        "output_rate_per_million": 2.0, "max_cost": 4.0,
        "pricing_revision": "offline-price", "currency": "USD",
    }
    settings = automatic_evaluation_settings(
        production_env(PR_EVALUATION_SETTINGS=json.dumps(overrides))
    )
    assert settings.max_reads == 5 and settings.max_source_bytes == 10000
    assert settings.retries == 0 and settings.max_cost == 4.0
    assert settings.input_rate_per_million == 1.0
    baseline = azure_production_settings().model_dump()
    assert {k: v for k, v in baseline.items() if k not in AZURE_PRODUCTION_PROFILE} == {
        k: v for k, v in EvaluationSettings().model_dump().items()
        if k not in AZURE_PRODUCTION_PROFILE
    }


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_signed_automatic_webhook_persists_exact_production_profile(env, action):
    dispatcher = SimpleNamespace(dispatch=AsyncMock())
    env.app.state.evaluation_orchestrator = EvaluationOrchestrator(
        env.store, env.worker, dispatcher=dispatcher,
        settings_factory=lambda: automatic_evaluation_settings(production_env()),
    )
    with TestClient(env.app) as client:
        response = deliver(client, env.event.model_copy(update={"source_event": action}))
    assert response.status_code == 202
    job = env.store.get(response.json()["evaluation_job_id"])
    effective = EvaluationSettings.model_validate_json(job.settings_json)
    assert effective == azure_production_settings()
    assert effective.model_dump(include=set(AZURE_PRODUCTION_PROFILE)) == {
        "token_encoding": "o200k_base", "profile": "DEFAULT", "max_attempts": 43,
        "max_tokens": 1075000, "deadline_seconds": 1500.0, "concurrency": 2,
        "max_input_tokens": 20000, "max_output_tokens": 5000,
        "model_window": 32768, "provider_reserve": 1024,
    }
    dispatcher.dispatch.assert_awaited_once_with(job.id)
    assert not env.model.requests


def test_missing_profile_blocks_admission_without_breaking_recovery(env):
    # Construction and recovery must not read the job-creation configuration.
    resolver = lambda: automatic_evaluation_settings({"K_SERVICE": "api"})
    service = EvaluationOrchestrator(
        env.store, None, settings_factory=resolver,
        dispatcher=SimpleNamespace(dispatch=AsyncMock()),
    )
    env.app.state.evaluation_orchestrator = service
    with TestClient(env.app) as client:
        response = deliver(client, env.event)
    assert response.status_code == 503
    assert "PR_EVALUATION_SETTINGS" in response.json()["detail"]
    assert env.store.list(REPO) == []


async def test_recovery_does_not_resolve_job_creation_settings(env):
    service = EvaluationOrchestrator(
        env.store, None,
        settings_factory=lambda: automatic_evaluation_settings({"K_SERVICE": "recovery"}),
        dispatcher=SimpleNamespace(dispatch=AsyncMock()),
    )
    await service.reconcile()


async def test_worker_runtime_uses_persisted_tokenizer_without_api_profile(env, monkeypatch):
    from ai_services.decision_guard.orchestration import runtime as module
    from ai_services.tests.test_decision_guard_context import Graph

    profile = azure_production_settings()
    job_id = env.store.enqueue(REPO, env.event, profile)
    monkeypatch.setenv("PR_EVALUATION_SETTINGS", "invalid-but-irrelevant-to-worker")
    monkeypatch.setattr(module.GitHubRepositorySource, "_run_git", lambda *args: None)
    monkeypatch.setattr(module.PullRequestGitReader, "read_revision", lambda *args: (env.event.base_sha, ()))
    runtime = ProductionRuntime(
        env.application_store, env.app.state.github_app, Graph(),
        lambda _: asyncio.Lock(), TrustedAuthority(), env.model,
    )
    async with runtime.open(env.store.get(job_id), env.event) as pipeline:
        assert pipeline.settings == profile
        encoding = tiktoken.get_encoding("o200k_base")
        sample = "Original policy evidence and source code. " * 100
        assert pipeline.token_counter(sample) == len(encoding.encode(sample))
        assert pipeline.token_counter(sample) < conservative_tokens(sample)
        assert pipeline.builder.token_counter is pipeline.token_counter
        assert pipeline.builder.request_budget.counter is pipeline.token_counter


async def test_real_singleton_fits_model_tokens_but_not_byte_estimate(tmp_path):
    # Preserve a complete governing policy and all original revision sources.
    rule = "Decision: All newly introduced Python local variables must use camelCase."
    policy = rule + "\n\n" + (
        "Original architecture context and governing rationale. " * 450
    )
    policy = policy.strip()
    context, source = await fixture(policy=rule, policy_document=policy)
    authority, _, _ = store_policy(tmp_path, policy, context)
    settings = azure_production_settings()
    counter = ModelTokenCounter.from_encoding(tiktoken.get_encoding("o200k_base"), model="o200k_base")
    receipts, _, _, bundle, _ = await prepare_evaluation_evidence(
        context, settings, source, authority,
    )
    units, _ = build_units(context, settings.max_units)
    budget = RequestBudget(settings, counter)
    packets, omitted = budget.pack(context, units, bundle, receipts)
    assert packets and not omitted
    assert all(m.input_tokens <= 20000 for m in budget.last_unit_measurements)
    byte_budget = RequestBudget(settings, conservative_tokens)
    _, byte_omitted = byte_budget.pack(context, units, bundle, receipts)
    assert byte_omitted  # The old estimator rejects the same intact source evidence.
    for request in packets:
        assert budget.count(request, "ASSESS") == counter(wire_text(request, "ASSESS")) + 1024
        if any(u.channel == "ADR" for u in request.units):
            assert any(e.kind == "ADR" and e.excerpt == policy for e in request.evidence)
        assert {"MERGE_BASE", "TARGET_BASE", "PR_HEAD"} <= {
            e.origin for e in request.evidence if e.kind == "CODE"
        }
    pipeline = ProductionPipeline(None, REPO, None, None, None, source,
                                  authority, settings, counter)
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await pipeline.evaluate(context, model)
    assert model.requests and model.challenge_requests
    assert any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    # Genuine overflow remains visible; changing the tokenizer cannot bypass it.
    small = settings.model_copy(update={"max_input_tokens": 1000})
    blocked = await ProductionPipeline(None, REPO, None, None, None, source,
                                      authority, small, counter).evaluate(context, ScriptedModel())
    assert not blocked.calls and blocked.outcome == "NOT_EVALUATED"
    assert blocked.coverage.omitted == len(blocked.plan)
    assert all("PACKET_TOO_LARGE" in u.reason_codes for u in blocked.units)


async def test_application_deadline_defers_challenge_and_preserves_checkpoint(env):
    context, source = await fixture()
    profile = azure_production_settings()
    counter = ModelTokenCounter.from_encoding(tiktoken.get_encoding("o200k_base"), model="o200k_base")
    now = [0.0]
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    original = model.assess

    async def assessment_uses_deadline(request, **kwargs):
        reply = await original(request, **kwargs)
        now[0] = 1501.0
        return reply

    model.assess = assessment_uses_deadline
    result = await DecisionIntegrityEvaluator(
        model, settings=profile, source=source, authority=TrustedAuthority(),
        token_counter=counter, clock=lambda: now[0],
    ).evaluate(context)
    assert model.requests and not model.challenge_requests
    assert not any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    assert result.coverage.scope != "COMPLETE"
    assert any(f.review_error == "EVALUATION_BUDGET" for f in result.findings)
    job_id = env.store.enqueue(REPO, env.event, profile)
    token = env.store.claim(job_id)
    env.store.checkpoint(job_id, token, "EVALUATED", result_json=result.model_dump_json())
    env.store.finish(job_id, token, result)
    job = env.store.get(job_id)
    assert job.stage == "EVALUATED" and job.status == "DEFERRED" and job.result_json


def test_saved_case_a_governing_rule_original_sources_and_omissions():
    path = Path(__file__).resolve().parents[3] / "frontend/tests/fixtures/stage-4-5-case-a.json"
    saved = json.loads(path.read_text(encoding="utf-8"))["detail"]["result"]
    result = EvaluationResult.model_validate_json(json.dumps(saved))
    records = json.loads(result.validation_records_json)
    readiness = next(r for r in records["requirements"] if r["requirement_id"].startswith("b9652205302a"))
    assert "only when all associated documents have achieved VERIFIED" in readiness["statement"]
    assert any(u.rule_id == readiness["requirement_id"] for u in result.plan)
    assert any(e.adr_id == "adr_917ed0fc62a7" and e.completeness == "COMPLETE"
               and len(e.excerpt) == 8330 for e in result.evidence)
    assert {"MERGE_BASE", "TARGET_BASE", "PR_HEAD"} <= {
        e.origin for e in result.evidence if e.kind == "CODE"
    }
    assert result.outcome == "CONFLICTS_FOUND" and result.coverage.scope == "PARTIAL"
    assert {"DECISION_LIMIT", "CONSTRAINT_LIMIT", "CONTEXT_TOKEN_LIMIT"} <= {
        l.code for l in result.limitations
    }
    assert sum(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings) == 3
    settings = azure_production_settings()
    assert settings.max_attempts > len(result.calls)
    assert settings.max_tokens > result.input_tokens + result.output_tokens
    assert settings.deadline_seconds * 1000 > result.elapsed_ms


async def test_context_settings_change_requires_rebuild_and_preserves_completed_work(env, github):
    limited = env.settings.model_copy(update={"max_attempts": 1})
    job_id = env.store.enqueue(REPO, env.event, limited)
    await env.worker.run_job(job_id)
    old = env.store.get(job_id)
    assert old.status == "DEFERRED" and old.context_json and old.provider_attempts == 1
    await CheckPublisher(env.store, github.app, enabled=True).publish(job_id)
    with env.sessions() as session:
        publication = session.get(CheckPublication, job_id)
        publication_id = publication.check_run_id
        packets = [p.request_json for p in session.scalars(select(ModelPacket).where(ModelPacket.job_id == job_id))]
    assert publication_id == "100"
    profile = azure_production_settings()
    with pytest.raises(ValueError, match="rebuild_context=True"):
        env.store.continue_job(job_id, settings=profile)
    assert env.store.get(job_id).settings_json == old.settings_json
    env.store.continue_job(job_id, settings=profile, rebuild_context=True)
    queued = env.store.get(job_id)
    assert queued.context_json is None and queued.stage == "CONTINUING"
    assert queued.changes_json == old.changes_json and queued.result_json == old.result_json
    assert queued.provider_attempts == old.provider_attempts
    assert queued.charged_tokens == old.charged_tokens and queued.charged_cost == old.charged_cost
    with env.sessions() as session:
        assert packets == [p.request_json for p in session.scalars(select(ModelPacket).where(ModelPacket.job_id == job_id))]
        assert session.get(CheckPublication, job_id).check_run_id == publication_id
    await env.worker.run_job(job_id)
    assert env.runtime.ingestions == 1 and env.runtime.contexts == 2
    assert len(env.model.requests) == len(env.model.challenge_requests) == 1
    # Settings remain authoritative even though the local environment has no profile.
    assert env.store.get(job_id).settings_json == profile.model_dump_json()
    await CheckPublisher(env.store, github.app, enabled=True, required_check_id=publication_id).publish(job_id)
    assert [write[0] for write in github.writes] == ["POST", "PATCH"]
    assert github.writes[-1][1].endswith("/check-runs/100") and len(github.runs) == 1


async def test_http_timeout_includes_admission_wait(monkeypatch):
    from ai_services.runs import evaluation_service as service
    from ai_services.runs import run_pr_evaluation

    monkeypatch.setattr(service, "require_enabled", lambda: None)
    monkeypatch.setattr(service, "slot", asyncio.Semaphore(0))
    run = AsyncMock()
    monkeypatch.setattr(run_pr_evaluation, "run", run)
    original_timeout = asyncio.timeout

    def bounded_timeout(seconds):
        assert seconds == 1680
        assert azure_production_settings().deadline_seconds + 180 <= seconds < 1800
        return original_timeout(0.01)

    monkeypatch.setattr(service.asyncio, "timeout", bounded_timeout)
    with pytest.raises(HTTPException, match="recovery pending") as error:
        await service.evaluate(service.EvaluationRequest(job_id="a" * 32))
    assert error.value.status_code == 503
    run.assert_not_awaited()
