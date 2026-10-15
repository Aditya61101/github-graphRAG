"""Offline tokenizer selection, phase accounting and explicit continuation rebuilds."""

# ruff: noqa: F811 -- imported pytest fixture
import json

import pytest
import tiktoken
from openai.lib._parsing._completions import type_to_response_format_param
from pydantic import ValidationError

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation.engine import RequestBudget
from ai_services.decision_guard.evaluation.model_adapter import (
    ASSESS_PROMPT,
    CHALLENGE_PROMPT,
)
from ai_services.decision_guard.evaluation.models import EvaluationSettings
from ai_services.decision_guard.evaluation.wire import (
    WireAssessment,
    WireChallenge,
    provider_request_json,
)
from ai_services.decision_guard.orchestration.runtime import ProductionPipeline
from ai_services.tests.test_decision_guard_context import REPO
from ai_services.tests.test_decision_guard_evaluation import (
    ScriptedModel,
    TrustedAuthority,
    fixture,
)
from ai_services.tests.test_pr_orchestration import env  # noqa: F401


@pytest.mark.parametrize(
    "encoding", ["", "cl100k_base", "o200k_harmony", "unknown", False, 1, {}]
)
def test_invalid_tokenizer_never_falls_back(encoding):
    with pytest.raises(ValidationError):
        EvaluationSettings(token_encoding=encoding)


def test_explicit_tokenizer_roundtrip_and_gpt54_mapping():
    settings = EvaluationSettings(token_encoding="o200k_base", max_tokens=1)
    assert (
        EvaluationSettings.model_validate_json(settings.model_dump_json()) == settings
    )
    assert EvaluationSettings().token_encoding is None
    assert tiktoken.encoding_name_for_model("gpt-5.4") == settings.token_encoding
    encoding = tiktoken.get_encoding(settings.token_encoding)
    counter = ModelTokenCounter.from_encoding(encoding, model=settings.token_encoding)
    for text in ("def f():\n    return 'नमस्ते世界'", "<|endoftext|>", " ", ""):
        assert counter(text) == len(encoding.encode(text, disallowed_special=()))


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [False, True])
async def test_production_pipeline_uses_counter_in_assessment_and_challenge(locked):
    context, source = await fixture()
    encoding = tiktoken.get_encoding("o200k_base")
    counted = []

    def count(text):
        counted.append(text)
        return len(encoding.encode(text, disallowed_special=()))

    counter = ModelTokenCounter("model:o200k_base", count)
    settings = EvaluationSettings(
        token_encoding="o200k_base",
        max_tokens=1 if locked else 200000,
        max_input_tokens=20000,
        provider_reserve=1024,
    )
    pipeline = ProductionPipeline(
        None, REPO, None, None, None, source, TrustedAuthority(), settings, counter
    )
    model = ScriptedModel({("ADR", "service.py"): "conflict"})
    result = await pipeline.evaluate(context, model)
    assert any(text.startswith(ASSESS_PROMPT) for text in counted)
    assert result.revision == context.revision
    if locked:
        assert not model.requests and not model.challenge_requests and not result.calls
        assert result.coverage.assessed == 0 and result.coverage.omitted > 0
        assert result.outcome == "NOT_EVALUATED"
    else:
        assert model.requests and model.challenge_requests
        assert any(text.startswith(CHALLENGE_PROMPT) for text in counted)
        budget = RequestBudget(settings, counter)
        for phase, requests in (
            ("ASSESS", model.requests),
            ("CHALLENGE", model.challenge_requests),
        ):
            for request in requests:
                prompt = ASSESS_PROMPT if phase == "ASSESS" else CHALLENGE_PROMPT
                schema = WireAssessment if phase == "ASSESS" else WireChallenge
                # Include the SDK's actual strict schema and an extra framing allowance.
                # No SDK client or provider request is constructed.
                sdk_estimate = (
                    sum(
                        counter(text)
                        for text in (
                            prompt,
                            provider_request_json(request, phase),
                            json.dumps(
                                type_to_response_format_param(schema),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                    )
                    + 32
                )
                assert budget.count(request, phase) >= sdk_estimate
                assert (
                    budget.count(request, phase) + settings.max_output_tokens
                    <= settings.model_window
                )
        assert result.findings
        assert result.findings[0].disposition == "SUPPORTED_CONFLICT"


@pytest.mark.asyncio
async def test_tokenizer_change_explicit_continuation_rebuilds_old_checkpoint_under_lock(
    env,
):
    locked = EvaluationSettings(max_tokens=1, max_input_tokens=30000)
    job_id = env.store.enqueue(REPO, env.event, locked)
    await env.worker.run_job(job_id)
    old = env.store.get(job_id)
    assert old.status == "DEFERRED" and old.context_json and old.result_json
    prior_changes = old.changes_json
    new = EvaluationSettings(
        token_encoding="o200k_base",
        max_tokens=1,
        max_input_tokens=20000,
        provider_reserve=1024,
    )
    env.store.continue_job(job_id, settings=new, rebuild_context=True)
    queued = env.store.get(job_id)
    assert queued.context_json is None
    assert queued.stage == "CONTINUING"  # excludes the old EVALUATED result shortcut
    assert queued.changes_json == prior_changes
    assert json.loads(queued.settings_json)["token_encoding"] == "o200k_base"
    await env.worker.run_job(job_id)
    assert env.runtime.contexts == 2 and env.runtime.ingestions == 1
    assert env.store.get(job_id).provider_attempts == 0
    assert not env.model.requests and not env.model.challenge_requests
