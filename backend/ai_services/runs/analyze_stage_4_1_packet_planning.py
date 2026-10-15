"""Offline analysis of preserved requests and exact alternative packet costs."""

import argparse
import asyncio
import itertools
import json
import re
import time
from pathlib import Path

import tiktoken

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import (
    ContextRequestBudget,
    RequestBudget,
    build_units,
    make_request,
)
from ai_services.decision_guard.evaluation.models import AssessmentRequest
from ai_services.decision_guard.evaluation.wire import provider_request_json
from ai_services.decision_guard.models import ArchitectureContext
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

CONFIGS = (
    "step1_baseline",
    "structural_only",
    "structural_bm25_scope",
    "full_semantic_unavailable",
    "full_controlled_mock",
)


def responsibility(u):
    return (u.channel, u.rule_id, u.path, u.delta_ids)


async def analyze(tag):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe tag")
    target = ROOT / f"A_{tag}_packet_analysis.json"
    if target.exists():
        raise FileExistsError("Use a fresh analysis tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(case["repository_id"], Path(case["source_root"]))
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    settings = EvaluationSettings.model_validate(manifest["settings"])
    encoding = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(encoding.encode(t, disallowed_special=())),
    )
    result, originals = {}, {}
    for name in CONFIGS:
        ctx = ArchitectureContext.model_validate_json(
            (ROOT / f"A_step2c_release_{name}_context.json").read_text(encoding="utf-8")
        )
        preserved = [
            AssessmentRequest.model_validate_json(json.dumps(p))
            for p in json.loads(
                (ROOT / f"A_step2c_release_{name}_packets.json").read_text(
                    encoding="utf-8"
                )
            )
        ]
        budget = ContextRequestBudget(
            source, authority, settings=settings, token_counter=counter
        )
        await asyncio.to_thread(budget, ctx)
        units, _ = build_units(ctx, settings.max_units)
        bundle, receipts = budget.last_bundle, budget.last_receipts
        index = {u.unit_id: i for i, u in enumerate(units)}
        if len(units) > 10:
            raise ValueError("Exhaustive offline diagnostic is limited to ten units")

        def request(group, ctx=ctx, units=units, bundle=bundle, receipts=receipts):
            return make_request(ctx, tuple(units[i] for i in group), bundle, receipts)

        def count(group, budget=budget, request=request):
            return budget.count(request(group), "ASSESS")

        subsets = {}
        for k in range(1, min(settings.max_batch_units, len(units)) + 1):
            for group in itertools.combinations(range(len(units)), k):
                subsets[group] = count(group)
        fits = {g: v for g, v in subsets.items() if v <= budget.input_limit}
        # Exact small-fixture partition lower bound, never used as production ranking.
        cache = {0: (0, 0, ())}

        def partition(mask, cache=cache, fits=fits):
            if mask in cache:
                return cache[mask]
            first = (mask & -mask).bit_length() - 1
            choices = []
            for group, cost in fits.items():
                bits = sum(1 << i for i in group)
                if first in group and mask & bits == bits:
                    n, res, groups = partition(mask ^ bits)
                    choices.append(
                        (
                            n + 1,
                            res + cost + settings.max_output_tokens,
                            (group, *groups),
                        )
                    )
            cache[mask] = min(choices) if choices else (999, 0, ())
            return cache[mask]

        best = partition((1 << len(units)) - 1)
        old_groups = [tuple(index[u.unit_id] for u in p.units) for p in preserved]
        exact_reproduction = [p.model_dump(mode="json") for p in preserved] == [
            p.model_dump(mode="json") for p in budget.last_packets
        ]
        singles = [request((i,)) for i in range(len(units))]
        wires = [json.loads(provider_request_json(p, "ASSESS")) for p in singles]
        originals[name] = (ctx, units, singles, wires)
        result[name] = {
            "preserved_packets_exactly_reproduced": exact_reproduction,
            "old_groups": old_groups,
            "old_inputs": [budget.count(p, "ASSESS") for p in preserved],
            "responsibilities": [responsibility(u) for u in units],
            "unit_dependencies": [
                u.model_dump(mode="json", exclude={"unit_id"}) for u in singles[0].units
            ]
            if len(singles) == 1
            else [
                p.units[0].model_dump(mode="json", exclude={"unit_id"}) for p in singles
            ],
            "single_inputs": [count((i,)) for i in range(len(units))],
            "feasible_groups": [
                {"group": g, "input": v} for g, v in fits.items() if len(g) > 1
            ],
            "optimal_packets": best[0],
            "optimal_reservation": best[1],
            "optimal_groups": best[2],
            "adjacent_pair_inputs": [count((i, i + 1)) for i in range(len(units) - 1)],
            "wire_component_tokens": [
                {
                    k: counter(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
                    for k, v in w.items()
                }
                for w in wires
            ],
        }
        planner = RequestBudget(settings, counter)
        started = time.perf_counter()
        planned, omitted = planner.pack(ctx, units, bundle, receipts)
        elapsed = time.perf_counter() - started
        assert not omitted
        result[name].update(
            planned_groups=[tuple(index[u.unit_id] for u in p.units) for p in planned],
            planned_inputs=[planner.count(p, "ASSESS") for p in planned],
            planning_seconds=elapsed,
            planned_reservation=sum(
                planner.count(p, "ASSESS") + settings.max_output_tokens for p in planned
            ),
        )
        print(
            json.dumps(
                {
                    "configuration": name,
                    **{
                        k: result[name][k]
                        for k in (
                            "preserved_packets_exactly_reproduced",
                            "old_groups",
                            "old_inputs",
                            "single_inputs",
                            "adjacent_pair_inputs",
                            "optimal_packets",
                            "optimal_reservation",
                            "optimal_groups",
                        )
                    },
                }
            ),
            flush=True,
        )
    _base_ctx, _base_units, base_single, base_wires = originals["step1_baseline"]
    comparisons = {}
    for name, (ctx, units, singles, wires) in originals.items():
        by_key = {
            responsibility(p.units[0]): (p, w)
            for p, w in zip(singles, wires, strict=True)
        }
        differences = []
        for p, w in zip(base_single, base_wires, strict=True):
            pair = by_key.get(responsibility(p.units[0]))
            if pair:
                q, v = pair
                canonical = [
                    k for k in type(p).model_fields if getattr(p, k) != getattr(q, k)
                ]
                # Normalize order only for actual architectural records; fingerprints
                # and diagnostic differences remain measured in original requests.
                record_equal = {
                    key: sorted(
                        json.loads(p.context_records_json)[key],
                        key=lambda x: json.dumps(x, sort_keys=True),
                    )
                    == sorted(
                        json.loads(q.context_records_json)[key],
                        key=lambda x: json.dumps(x, sort_keys=True),
                    )
                    for key in json.loads(p.context_records_json)
                }
                differences.append(
                    {
                        "rule_id": p.units[0].rule_id,
                        "canonical_differing_fields": canonical,
                        "evidence_exactly_equal": p.evidence == q.evidence,
                        "observations_equal": p.observations == q.observations,
                        "policy_observations_equal": p.policy_observations
                        == q.policy_observations,
                        "unit_dependencies_equal": p.units[0].model_dump(
                            exclude={"unit_id"}
                        )
                        == q.units[0].model_dump(exclude={"unit_id"}),
                        "provider_differing_fields": [k for k in w if w[k] != v[k]],
                        "canonical_record_equality": record_equal,
                    }
                )
        comparisons[name] = differences
    assert all(file_hash(ROOT / name) == sha for name, sha in protected.items())
    save(
        target,
        {
            "offline": True,
            "billable_calls": 0,
            "settings": settings.model_dump(mode="json"),
            "configurations": result,
            "comparisons_with_baseline": comparisons,
            "protected_files": len(protected),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    asyncio.run(analyze(parser.parse_args().tag))
