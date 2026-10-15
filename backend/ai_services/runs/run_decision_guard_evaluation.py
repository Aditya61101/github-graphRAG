"""Opt-in, bounded Stage 4.2 smoke harness. Dry-run never creates an API client.

Run from backend with `python -m ai_services.runs.run_decision_guard_evaluation
--context ../instructions/4_1_scope_enriched_context.json`. Add --live explicitly
to spend API tokens. This harness does not publish results or mutate databases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ai_services.decision_guard.evaluation import (
    AzureEvaluationModel,
    DecisionIntegrityEvaluator,
    EvaluationSettings,
    UploadAuthority,
)
from ai_services.decision_guard.evaluation.engine import build_units
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.models import ArchitectureContext


class ReadOnlyStore:
    def __init__(self, path):
        self.uri = Path(path).resolve().as_uri() + "?mode=ro"

    def _get(self, table, identity):
        # table is selected by the two fixed methods below, never by model input.
        with sqlite3.connect(self.uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                f"SELECT * FROM {table} WHERE id = ?", (identity,)
            ).fetchone()
            return SimpleNamespace(**dict(row)) if row else None

    def get_repository(self, identity):
        return self._get("repositories", identity)

    def get_adr(self, identity):
        return self._get("adrs", identity)


async def run(args):
    context = validate_context(
        ArchitectureContext.model_validate_json(
            args.context.read_text(encoding="utf-8")
        )
    )
    settings = EvaluationSettings(
        profile="CUSTOM",
        max_attempts=args.max_attempts,
        max_tokens=args.max_tokens,
        deadline_seconds=args.deadline,
        concurrency=1,
        max_input_tokens=args.max_input_tokens,
        model_window=args.model_window,
    )
    # Harness is intentionally narrower than DEFAULT; EXPANDED is an application choice.
    if args.max_attempts > 8 or args.max_tokens > 60000 or args.deadline > 120:
        raise ValueError(
            "Smoke harness cannot exceed DEFAULT attempt/token/time ceilings"
        )
    units, overflow = build_units(context, settings.max_units)
    if not args.live:
        print(
            json.dumps(
                {
                    "live": False,
                    "model_calls": 0,
                    "context_hash": context.context_hash,
                    "units": len(units),
                    "overflow": overflow,
                    "settings": settings.model_dump(),
                },
                indent=2,
            )
        )
        return
    if not args.deployment_revision:
        raise ValueError("--deployment-revision is required for live evaluation")
    if bool(args.database) != bool(args.adr_storage):
        raise ValueError(
            "Provide both --database and --adr-storage for upload authority"
        )
    if args.output and args.output.resolve() == args.context.resolve():
        raise ValueError("Output must not overwrite the input context")
    from openai import AsyncAzureOpenAI

    from shared.utils.llm_utils import AzureOpenAILLM

    client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        max_retries=0,
    )
    try:
        model = AzureEvaluationModel(
            AzureOpenAILLM(client, os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]),
            deployment_revision=args.deployment_revision,
            output_parameter=args.output_parameter,
        )
        authority = (
            UploadAuthority(ReadOnlyStore(args.database), args.adr_storage)
            if args.database
            else None
        )
        result = await DecisionIntegrityEvaluator(
            model, settings=settings, authority=authority
        ).evaluate(context)
        rendered = result.model_dump_json(indent=2)
        if args.output:
            # Exclusive creation protects existing local reports.
            with args.output.open("x", encoding="utf-8") as f:
                f.write(rendered + "\n")
        else:
            print(rendered)
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--deployment-revision")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=60000)
    parser.add_argument("--deadline", type=float, default=120.0)
    parser.add_argument(
        "--max-input-tokens",
        type=int,
        default=16000,
        help="Conservative UTF-8 byte envelope; oversized packets remain unassessed",
    )
    parser.add_argument("--model-window", type=int, default=32768)
    parser.add_argument(
        "--output-parameter",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_completion_tokens",
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--adr-storage", type=Path)
    parser.add_argument("--output", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
