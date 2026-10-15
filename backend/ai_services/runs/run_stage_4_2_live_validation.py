"""Explicit opt-in validation with one persistent 8-attempt/60k-token ledger.

Run without --live to check prepared contexts. Expected outcomes are never loaded
here. Repeated live invocations resume the same ledger; budgets cannot be reset
through CLI flags. This is local validation tooling, not Stage 4.3 job storage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import tiktoken
from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.evaluation.model_adapter import wire_text
from ai_services.decision_guard.evaluation.models import DispatchDenied
from ai_services.decision_guard.models import ArchitectureContext
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureStore,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from dotenv import load_dotenv


class AggregateBudgetExhausted(DispatchDenied):
    def __init__(self, message):
        super().__init__("BUDGET_DENIED")


class AggregateModel:
    """Reserve durably before dispatch; lost replies retain the full reservation."""

    def __init__(self, model, directory, counter, *, max_tokens=60000):
        self.model, self.directory, self.counter = model, Path(directory), counter
        self.max_tokens = max_tokens
        self.deployment_revision = model.deployment_revision
        self.path = self.directory / "aggregate_usage.json"
        self.lock = asyncio.Lock()
        self.case = ""
        self.run_tag = None
        self.state = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "max_attempts": 8,
                "max_tokens": max_tokens,
                "deployment_revision": self.deployment_revision,
                "calls": [],
            }
        )
        if (
            self.state["max_attempts"],
            self.state["max_tokens"],
            self.state["deployment_revision"],
        ) != (8, max_tokens, self.deployment_revision):
            raise ValueError("Aggregate ledger policy/deployment mismatch")

    @property
    def remaining_attempts(self):
        return max(0, 8 - len(self.state["calls"]))

    @property
    def remaining_tokens(self):
        return max(
            0, self.max_tokens - sum(c["charged_tokens"] for c in self.state["calls"])
        )

    def persist(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    async def _call(self, request, phase, **kwargs):
        async with self.lock:
            reserve = (
                self.counter(wire_text(request, phase))
                + 512
                + kwargs["max_output_tokens"]
            )
            if not self.remaining_attempts or reserve > self.remaining_tokens:
                raise AggregateBudgetExhausted("Aggregate validation limit reached")
            index = len(self.state["calls"])
            entry = {
                "case": self.case,
                "run_tag": self.run_tag,
                "phase": phase,
                "reserved_tokens": reserve,
                "charged_tokens": reserve,
                "status": "DISPATCH_RESERVED",
            }
            self.state["calls"].append(entry)
            self.persist()
            prefix = self.directory / f"call_{index + 1:02d}"
            prefix.with_suffix(".request.json").write_text(
                request.model_dump_json(indent=2), encoding="utf-8"
            )
            start = time.monotonic()
            try:
                method = (
                    self.model.assess if phase == "ASSESS" else self.model.challenge
                )
                reply = await method(request, **kwargs)
                prefix.with_suffix(".reply.json").write_text(
                    reply.model_dump_json(indent=2), encoding="utf-8"
                )
                entry.update(
                    status=reply.status,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    request_id=reply.request_id,
                )
                if reply.input_tokens is not None and reply.output_tokens is not None:
                    entry["charged_tokens"] = reply.input_tokens + reply.output_tokens
                return reply
            except DispatchDenied as exc:
                entry.update(
                    status=exc.reason,
                    charged_tokens=0,
                    denied_reservation_tokens=reserve,
                )
                raise
            except BaseException as exc:
                # Preserve failed original attempts without exposing provider secrets.
                entry["status"] = "ERROR:" + type(exc).__name__
                raise
            finally:
                entry["elapsed_ms"] = int((time.monotonic() - start) * 1000)
                self.persist()

    async def assess(self, request, **kwargs):
        return await self._call(request, "ASSESS", **kwargs)

    async def challenge(self, request, **kwargs):
        return await self._call(request, "CHALLENGE", **kwargs)


async def run(args):
    run_tag = getattr(args, "run_tag", None)
    if run_tag and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", run_tag):
        raise ValueError("Invalid local run tag")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    selected = [c for c in manifest["cases"] if not args.case or c["id"] == args.case]
    if args.live and len(selected) != 1:
        raise ValueError("Live validation requires exactly one selected case")
    contexts = {
        c["id"]: validate_context(
            ArchitectureContext.model_validate_json(
                (ROOT / (c["id"] + "_context.json")).read_text(encoding="utf-8")
            )
        )
        for c in selected
    }
    if not args.live:
        print(
            json.dumps(
                {
                    "live": False,
                    "real_model_calls": 0,
                    "deployment": manifest["deployment"],
                    "aggregate_max_attempts": 8,
                    "aggregate_max_tokens": 60000,
                    "cases": [
                        {
                            "id": c["id"],
                            "context_hash": contexts[c["id"]].context_hash,
                            "estimated_assessment_input": c["estimated_input_tokens"],
                            "batches": c["assessment_batches"],
                        }
                        for c in selected
                    ],
                },
                indent=2,
            )
        )
        return
    load_dotenv(".env")
    if os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"] != manifest["deployment"]:
        raise ValueError("Deployment differs from reviewed dry-run manifest")
    # Exclusive local process lease prevents simultaneous exercises sharing a ledger.
    lease = ROOT / "live_validation.lock"
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    client = None
    try:
        import httpx
        from openai import AsyncAzureOpenAI
        from shared.utils.llm_utils import AzureOpenAILLM

        async def capture_response(response):
            # Preserve the original provider body, including parsing failures.
            # No request/response headers or credentials are written.
            await response.aread()
            index = len(aggregate.state["calls"])
            (ROOT / f"call_{index:02d}.provider.json").write_bytes(response.content)

        client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ["AZURE_OPENAI_API_VERSION"],
            max_retries=0,
            http_client=httpx.AsyncClient(event_hooks={"response": [capture_response]}),
        )
        adapter = AzureEvaluationModel(
            AzureOpenAILLM(client, manifest["deployment"]),
            deployment_revision=args.deployment_revision,
        )
        enc = tiktoken.get_encoding("o200k_base")
        counter = ModelTokenCounter(
            "o200k_base:deployment-estimate",
            lambda t: len(enc.encode(t, disallowed_special=())),
        )
        aggregate = AggregateModel(adapter, ROOT, counter)
        for case in selected:
            name = case["id"]
            result_path = ROOT / (
                name + ("_" + run_tag if run_tag else "") + "_result.json"
            )
            if result_path.exists():
                continue
            if not aggregate.remaining_attempts or not aggregate.remaining_tokens:
                print(name + ": not evaluated; aggregate budget exhausted")
                continue
            ctx = contexts[name]
            source, _ = source_for(
                ctx.revision.repository_id, Path(case["source_root"])
            )
            authority = (
                UploadAuthority(
                    FixtureStore(ROOT / "independent_authority.json"),
                    ROOT / "fixture_adrs",
                )
                if name == "D"
                else UploadAuthority(
                    ReadOnlyStore("data/decisionguard.db"), "data/adrs"
                )
            )
            settings = EvaluationSettings.model_validate(
                {
                    **manifest["settings"],
                    "max_attempts": min(2, aggregate.remaining_attempts),
                    "max_tokens": aggregate.remaining_tokens,
                }
            )
            aggregate.case = name
            aggregate.run_tag = run_tag
            result = await DecisionIntegrityEvaluator(
                aggregate,
                settings=settings,
                source=source,
                authority=authority,
                token_counter=counter,
            ).evaluate(ctx)
            with result_path.open("x", encoding="utf-8") as f:
                f.write(result.model_dump_json(indent=2))
            print(
                json.dumps(
                    {
                        "case": name,
                        "outcome": result.outcome,
                        "coverage": result.coverage.model_dump(),
                        "remaining_attempts": aggregate.remaining_attempts,
                        "remaining_tokens": aggregate.remaining_tokens,
                    }
                )
            )
    finally:
        if client is not None:
            await client.close()
        lease.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", choices=("A", "B", "C", "D"))
    parser.add_argument(
        "--run-tag",
        help="New local result name for an approved rerun; never resets the aggregate ledger",
    )
    parser.add_argument("--deployment-revision")
    args = parser.parse_args()
    if args.live and not args.deployment_revision:
        parser.error("Live execution requires --deployment-revision")
    if args.live and not args.case:
        parser.error("Live execution requires --case; only one scenario runs")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
