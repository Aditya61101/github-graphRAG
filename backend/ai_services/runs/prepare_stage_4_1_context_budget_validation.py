"""Offline full-archive context/request sizing. Never constructs a model client.

Only new local artifacts are written, exclusively; historical fixtures and the
shared Azure usage ledger are never changed. Frozen readiness is fixture-only.
"""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import tiktoken

from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.evaluation.model_adapter import PROMPT_VERSION
from ai_services.decision_guard.evaluation.wire import provider_request_json
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureReadiness,
    FrozenGraph,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore


class FullArchiveGraph(FrozenGraph):
    """Original archived rows plus the scoped declaration-source read port."""

    def entity_chunks(self, ids, limit):
        rows = [
            {**r, "entities": [e for e in r["entities"] if e["id"] in ids]}
            for r in self.payload["chunks"]
            if any(e["id"] in ids for e in r["entities"])
        ]

        def rank(row):
            text = row["chunk"]["text"]
            declared = any(
                "def " + e["name"] + "(" in text
                or "class " + e["name"] in text
                or "function " + e["name"] + "(" in text
                for e in row["entities"]
            )
            return not declared, row["path"], row["chunk"]["id"]

        return self.page(sorted(rows, key=rank), limit)


def save(path, data):
    with path.open("x", encoding="utf-8") as file:
        file.write(json.dumps(data, indent=2, ensure_ascii=False))


async def prepare(tag):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe artifact tag")
    context_path = ROOT / f"A_{tag}_context.json"
    if context_path.exists():
        raise FileExistsError("Prepared artifact already exists; use a new offline tag")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    archive_path = ROOT / "claimiq_graph_snapshot.json"
    assert digest(archive_path.read_text(encoding="utf-8")) == manifest["snapshot_hash"]
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    changes = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(changes.repository_id, Path(case["source_root"]))
    settings = EvaluationSettings.model_validate(manifest["settings"])
    encoding = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda text: len(encoding.encode(text, disallowed_special=())),
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    sizing = ContextRequestBudget(
        source, authority, settings=settings, token_counter=counter
    )
    context = validate_context(
        await ArchitectureContextBuilder(
            FullArchiveGraph(archive),
            source,
            FixtureReadiness(
                changes.repository_id,
                changes.base_sha,
                manifest["snapshot_hash"],
                changes.github_repository_id,
                changes.base_branch,
            ),
            lambda _: asyncio.Lock(),
            ContextSettings(
                max_context_tokens=24000, max_decisions=4, max_constraints=8
            ),
            token_counter=counter,
            request_budget=sizing,
        ).build(changes)
    )
    original = validate_context(
        ArchitectureContext.model_validate_json(
            (ROOT / "A_context.json").read_text(encoding="utf-8")
        )
    )
    assert context.revision == original.revision
    assert {(a.adr_id, a.content_hash) for a in context.adr_snapshot} == {
        (a.adr_id, a.content_hash) for a in original.adr_snapshot
    }
    # Comparison only, after deterministic selection. No expected-outcome input
    # or known-requirement priority is given to the builder or packet budget.
    original_rule = original.requirements[0].requirement_id
    selected = next(
        (r for r in context.requirements if r.requirement_id == original_rule), None
    )
    packet_info = []
    for index, request in enumerate(sizing.last_packets):
        provider = json.loads(provider_request_json(request, "ASSESS"))
        input_tokens = sizing.count(request, "ASSESS")
        packet_info.append(
            {
                "units": len(request.units),
                "input_tokens_including_reserve": input_tokens,
                "reserved_output_tokens": settings.max_output_tokens,
                "window_total": input_tokens + settings.max_output_tokens,
                "fits": sizing.fits(request, "ASSESS"),
                "model_diagnostic_tokens": counter(
                    json.dumps(provider["coverage"], separators=(",", ":"))
                ),
                "source_anchors": [
                    a.model_dump(mode="json") for a in request.source_anchors
                ],
                "policy_observations": [
                    o.model_dump(mode="json") for o in request.policy_observations
                ],
            }
        )
        save(ROOT / f"A_{tag}_assessment_{index}.json", request.model_dump(mode="json"))
        save(ROOT / f"A_{tag}_provider_input_{index}.json", provider)
    model_projection = json.loads(model_context_json(context))
    ledger = json.loads((ROOT / "aggregate_usage.json").read_text(encoding="utf-8"))
    metric = {
        "live": False,
        "real_model_calls": 0,
        "prompt_version": PROMPT_VERSION,
        "context_builder_version": context.context_builder_version,
        "context_hash": context.context_hash,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "context_tokens": counter(model_context_json(context)),
        "audit_context_tokens": counter(context.model_dump_json()),
        "model_context_diagnostic_tokens": counter(
            json.dumps(
                model_projection["coverage"]["limitations"], separators=(",", ":")
            )
        ),
        "retained_counts": {
            "requirements": len(context.requirements),
            "entities": len(context.entities),
            "relationships": len(context.relationships),
            "evidence": len(context.evidence),
            "deltas": len(context.deltas),
        },
        "requirements": [
            {
                "id": r.requirement_id,
                "score": r.relevance_score,
                "reasons": r.discovery_reasons,
                "statement": r.statement,
            }
            for r in context.requirements
        ],
        "original_rule_retained": bool(selected),
        "original_rule_support": list(selected.evidence_ids) if selected else [],
        "entities": [
            {"id": e.entity_id, "name": e.name, "paths": e.paths}
            for e in context.entities
        ],
        "omissions": dict(Counter(l.code for l in context.coverage.limitations)),
        "packets": packet_info,
        "input_limit": sizing.input_limit,
        "model_window": settings.model_window,
        "source_policy_limits_unchanged": True,
        "fixture_readiness_only": True,
        "aggregate_remaining_attempts": 8 - len(ledger["calls"]),
        "aggregate_remaining_tokens": 60000
        - sum(c["charged_tokens"] for c in ledger["calls"]),
        "assessment_reservation": sum(p["window_total"] for p in packet_info),
        "ready_for_assessment_packets": bool(packet_info)
        and all(p["fits"] for p in packet_info)
        and bool(selected),
    }
    save(context_path, context.model_dump(mode="json"))
    save(ROOT / f"A_{tag}_metrics.json", metric)
    print(json.dumps(metric, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="context_budget")
    asyncio.run(prepare(parser.parse_args().tag))


if __name__ == "__main__":
    main()
