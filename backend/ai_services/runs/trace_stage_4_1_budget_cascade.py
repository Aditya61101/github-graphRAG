"""Offline pruning iteration trace before the budget-preservation change."""

import asyncio
import json
import time
from pathlib import Path

import tiktoken

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    build_units,
    make_request,
)
from ai_services.decision_guard.hybrid import HybridContextRetrieval, RetrievalSettings
from ai_services.decision_guard.hybrid.models import SearchIndex
from ai_services.decision_guard.settings import ContextSettings
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.runs.benchmark_stage_4_1_hybrid_context import ArchiveContextGraph
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureReadiness,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore


async def main():
    output = ROOT / "A_budget_preservation_before_trace.json"
    if output.exists():
        raise FileExistsError("Preserve original trace")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    event = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    source, _ = source_for(
        event.repository_id,
        Path(next(c for c in manifest["cases"] if c["id"] == "A")["source_root"]),
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    encoding = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(encoding.encode(t, disallowed_special=())),
    )

    class Trace(ContextRequestBudget):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.trace = []

        def __call__(self, ctx):
            result = super().__call__(ctx)
            units, _ = build_units(ctx, self.settings.max_units)
            singles = []
            for u in units:
                req = make_request(ctx, (u,), self.last_bundle, self.last_receipts)
                singles.append(
                    {
                        "unit_id": u.unit_id,
                        "rule_id": u.rule_id,
                        "path": u.path,
                        "input_tokens": self.count(req, "ASSESS"),
                        "feasible": self.fits(req, "ASSESS"),
                    }
                )
            self.trace.append(
                {
                    "context_hash": ctx.context_hash,
                    "requirements": [
                        {"id": r.requirement_id, "priority": r.relevance_score}
                        for r in ctx.requirements
                    ],
                    "entities": [e.entity_id for e in ctx.entities],
                    "relationships": [r.relationship_id for r in ctx.relationships],
                    "minimum_units": singles,
                    "packets": [
                        {
                            "batch_id": p.batch_id,
                            "unit_ids": [u.unit_id for u in p.units],
                            "input_tokens": self.count(p, "ASSESS"),
                        }
                        for p in self.last_packets
                    ],
                    "omitted_unit_ids": result.omitted_unit_ids,
                    "global_max": result.input_tokens,
                    "fits": result.fits,
                }
            )
            return result

    trace = Trace(
        source,
        authority,
        settings=EvaluationSettings.model_validate(manifest["settings"]),
        token_counter=counter,
    )
    builder = ArchitectureContextBuilder(
        ArchiveContextGraph(
            json.loads(
                (ROOT / "claimiq_graph_snapshot.json").read_text(encoding="utf-8")
            )
        ),
        source,
        FixtureReadiness(
            event.repository_id,
            event.base_sha,
            manifest["snapshot_hash"],
            event.github_repository_id,
            event.base_branch,
        ),
        lambda _: asyncio.Lock(),
        settings=ContextSettings(max_decisions=4, max_constraints=8),
        token_counter=counter,
        request_budget=trace,
        hybrid=HybridContextRetrieval(
            index=SearchIndex.model_validate_json(
                (ROOT / "A_step2b_chunk_reuse_index.json").read_text(encoding="utf-8")
            ),
            authority=authority,
            mode="ARCHIVED",
            settings=RetrievalSettings(semantic=False),
        ),
    )
    start = time.monotonic()
    ctx = await builder.build(event)
    for current, nxt in zip(trace.trace, trace.trace[1:]):
        removed = {r["id"] for r in current["requirements"]} - {
            r["id"] for r in nxt["requirements"]
        }
        current["removed_requirements"] = sorted(removed)
        current["feasible_removed_requirements"] = [
            u["rule_id"]
            for u in current["minimum_units"]
            if u["rule_id"] in removed and u["feasible"]
        ]
        current["failing_units"] = [
            u for u in current["minimum_units"] if not u["feasible"]
        ]
    assert all(file_hash(ROOT / n) == h for n, h in protected.items())
    save(
        output,
        {
            "iterations": trace.trace,
            "runtime_seconds": time.monotonic() - start,
            "final_requirements": len(ctx.requirements),
            "historical_unchanged": True,
        },
    )
    print(
        json.dumps(
            {
                "iterations": len(trace.trace),
                "final_requirements": len(ctx.requirements),
                "removed_feasible_requirements": sum(
                    len(t.get("feasible_removed_requirements", [])) for t in trace.trace
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
