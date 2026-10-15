"""Local harness safety checks; no provider clients or live graph access."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_services.decision_guard.evaluation.models import ModelReply
from ai_services.runs.run_stage_4_2_live_validation import (
    AggregateBudgetExhausted,
    AggregateModel,
)


class Provider:
    deployment_revision = "test-deployment"

    def __init__(self, *, fail=False, usage=True):
        self.fail, self.usage = fail, usage
        self.calls = self.active = self.peak = 0

    async def assess(self, request, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.001)
            if self.fail:
                raise TimeoutError("Mock provider failure")
            return ModelReply(
                output_json=None,
                status="INVALID",
                input_tokens=100 if self.usage else None,
                output_tokens=20 if self.usage else None,
            )
        finally:
            self.active -= 1

    challenge = assess


def empty_assessment():
    return {
        "revision": {"repository_id": "fixture"},
        "units": [],
        "evidence": [],
        "context_records_json": json.dumps(
            {"deltas": [], "entities": [], "relationships": [], "requirements": []}
        ),
        "limitations": [],
        "adr_retrieval": "ABSENT",
        "adr_catalog_complete": True,
    }


REQUEST = SimpleNamespace(
    batch_id="b",
    candidates=(),
    model_dump_json=lambda **kwargs: "{}",
    model_dump=lambda **kwargs: {
        **empty_assessment(),
        "assessment": empty_assessment(),
        "candidates": [],
    },
    limitations=(),
    assessment=SimpleNamespace(
        limitations=(),
        evidence=(),
        context_hash="0" * 64,
        units=(),
        authority=(),
        observations=(),
        policy_observations=(),
        source_anchors=(),
        context_records_json=empty_assessment()["context_records_json"],
        adr_retrieval="ABSENT",
        adr_catalog_complete=True,
        revision=SimpleNamespace(
            model_dump=lambda **kwargs: empty_assessment()["revision"],
            repository_id="fixture",
            base_sha="a" * 40,
            head_sha="a" * 40,
            merge_base_sha="a" * 40,
        ),
    ),
)


async def test_aggregate_attempts_persist_across_runs_and_phases(tmp_path):
    provider = Provider()
    model = AggregateModel(provider, tmp_path, lambda _: 100)
    for _ in range(4):
        await model.assess(REQUEST, max_output_tokens=1000, timeout=1)
    resumed = AggregateModel(provider, tmp_path, lambda _: 100)
    for _ in range(4):
        await resumed.challenge(REQUEST, max_output_tokens=1000, timeout=1)
    with pytest.raises(AggregateBudgetExhausted):
        await resumed.assess(REQUEST, max_output_tokens=1000, timeout=1)
    assert provider.calls == 8 and resumed.remaining_attempts == 0
    assert resumed.remaining_tokens == 60000 - 8 * 120


@pytest.mark.parametrize("fail", [True, False])
async def test_unreported_usage_keeps_reservation_and_stops_dispatch(tmp_path, fail):
    provider = Provider(fail=fail, usage=False)
    model = AggregateModel(provider, tmp_path, lambda _: 40000)
    if fail:
        with pytest.raises(TimeoutError):
            await model.assess(REQUEST, max_output_tokens=5000, timeout=1)
    else:
        await model.assess(REQUEST, max_output_tokens=5000, timeout=1)
    resumed = AggregateModel(provider, tmp_path, lambda _: 40000)
    with pytest.raises(AggregateBudgetExhausted):
        await resumed.challenge(REQUEST, max_output_tokens=5000, timeout=1)
    assert provider.calls == 1 and resumed.remaining_tokens == 60000 - 45512


async def test_aggregate_serializes_provider_calls(tmp_path):
    provider = Provider()
    model = AggregateModel(provider, tmp_path, lambda _: 100)
    await asyncio.gather(
        model.assess(REQUEST, max_output_tokens=1000, timeout=1),
        model.challenge(REQUEST, max_output_tokens=1000, timeout=1),
    )
    assert provider.calls == 2 and provider.peak == 1


def test_ledger_deployment_cannot_change_on_resume(tmp_path):
    model = AggregateModel(Provider(), tmp_path, lambda _: 100)
    model.persist()
    provider = Provider()
    provider.deployment_revision = "another-deployment"
    with pytest.raises(ValueError, match="mismatch"):
        AggregateModel(provider, tmp_path, lambda _: 100)


async def test_explicit_authorized_ceiling_preserves_history_and_bounds_call(tmp_path):
    history = [
        {"charged_tokens": n, "status": "HISTORICAL"} for n in (15855, 16917, 20455)
    ]
    path = tmp_path / "aggregate_usage.json"
    path.write_text(
        json.dumps(
            {
                "max_attempts": 8,
                "max_tokens": 77335,
                "deployment_revision": "test-deployment",
                "calls": history,
            }
        ),
        encoding="utf-8",
    )
    provider = Provider(usage=False)
    with pytest.raises(ValueError, match="mismatch"):
        AggregateModel(provider, tmp_path, lambda _: 18596)
    assert json.loads(path.read_text())["calls"] == history
    model = AggregateModel(provider, tmp_path, lambda _: 18596, max_tokens=77335)
    assert model.remaining_tokens == 24108
    await model.assess(REQUEST, max_output_tokens=5000, timeout=1)
    assert model.state["calls"][:3] == history
    assert model.state["calls"][-1]["reserved_tokens"] == 24108
    with pytest.raises(AggregateBudgetExhausted):
        await model.assess(REQUEST, max_output_tokens=5000, timeout=1)
    assert provider.calls == 1 and model.remaining_tokens == 0


async def test_selected_case_dry_run_does_not_load_other_contexts(
    tmp_path, monkeypatch, capsys
):
    from ai_services.runs import run_stage_4_2_live_validation as runner
    from ai_services.tests.test_decision_guard_evaluation import fixture

    context, _ = await fixture()
    (tmp_path / "A_context.json").write_text(
        context.model_dump_json(), encoding="utf-8"
    )
    manifest = {
        "deployment": "test",
        "cases": [
            {"id": "A", "estimated_input_tokens": [100], "assessment_batches": 1},
            {"id": "B", "estimated_input_tokens": [100], "assessment_batches": 1},
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    await runner.run(SimpleNamespace(case="A", live=False))
    output = json.loads(capsys.readouterr().out)
    assert [case["id"] for case in output["cases"]] == ["A"]
    assert output["real_model_calls"] == 0
    assert not (tmp_path / "B_context.json").exists()


async def test_live_requires_one_case_before_loading_contexts(tmp_path, monkeypatch):
    from ai_services.runs import run_stage_4_2_live_validation as runner

    (tmp_path / "manifest.json").write_text(
        json.dumps({"cases": [{"id": "A"}, {"id": "B"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        await runner.run(SimpleNamespace(case=None, live=True))


async def test_rerun_tag_cannot_escape_local_result_directory(tmp_path, monkeypatch):
    from ai_services.runs import run_stage_4_2_live_validation as runner

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="run tag"):
        await runner.run(SimpleNamespace(case="A", live=False, run_tag="../original"))
