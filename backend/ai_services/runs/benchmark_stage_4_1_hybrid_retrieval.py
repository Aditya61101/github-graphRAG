"""Offline Step 2B ablations. No Azure client, live writes or context packing."""

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

import tiktoken

from ai_services.decision_guard import ArchitectureContextBuilder, PreliminaryDiscovery
from ai_services.decision_guard.evaluation import UploadAuthority
from ai_services.decision_guard.hybrid import (
    DerivedIndexBuilder,
    HybridRetriever,
    RetrievalSettings,
)
from ai_services.decision_guard.models import digest
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.runs.prepare_stage_4_1_context_budget_validation import (
    FullArchiveGraph,
)
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureReadiness,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.tests.test_hybrid_evidence_retrieval import prepared, runner


class ArchiveIndexGraph(FullArchiveGraph):
    def __init__(self, payload, vectors=None):
        super().__init__(payload)
        self.vectors = vectors

    def adr_chunks(self, adr_ids, limit):
        if self.vectors is None:
            raise ValueError("No frozen original ADR vector snapshot")
        originals = {}
        for rule in self.payload["requirements"]:
            for support in rule["support"]:
                originals.setdefault(support["chunk"]["id"], []).append(
                    {
                        "id": rule["knowledge"]["id"],
                        "repository": self.payload["repository_id"],
                        "adr_id": support["adr"]["id"],
                    }
                )
        catalogue = {
            r["adr"]["id"]: r["adr"]["content_hash"] for r in self.payload["catalog"]
        }
        rows = []
        for row in self.vectors["rows"]:
            a, c = row["adr"], row["chunk"]
            if a["id"] not in adr_ids:
                continue
            if (
                a["repository"] != self.payload["repository_id"]
                or catalogue.get(a["id"]) != a["content_hash"]
            ):
                raise ValueError(
                    "Existing vector snapshot escaped archived ADR identities"
                )
            # Use only archived SUPPORTS mappings; never import a different graph generation's requirements.
            rows.append(
                {"adr": a, "chunk": c, "requirements": originals.get(c["id"], [])}
            )
        return self.page(rows, limit)

    def code_inventory(self, limit):
        # Complete archived inventory; no expected IDs enter selection/ranking.
        return self.page(
            sorted(self.payload["chunks"], key=lambda r: (r["path"], r["chunk"]["id"])),
            limit,
        )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)


def scores(rows, expected):
    ranks = {c.original_id: c.rank for c in rows}
    relevant = sorted(expected)
    return {
        "expected_ids": relevant,
        "ranks": {identifier: ranks.get(identifier) for identifier in relevant},
        "recall_at_k": {
            str(k): sum(
                1 for identifier in relevant if 0 < ranks.get(identifier, 0) <= k
            )
            / len(relevant)
            for k in (1, 5, 10, 32, 64, 96)
        }
        if relevant
        else {},
        "reciprocal_first_relevant_rank": max(
            (1 / ranks[identifier] for identifier in relevant if identifier in ranks),
            default=0,
        ),
    }


CONFIGURATIONS = {
    "structural_only": RetrievalSettings(lexical=False, scope=False, semantic=False),
    "structural_bm25_scope": RetrievalSettings(semantic=False),
    "structural_semantic_scope": RetrievalSettings(lexical=False),
    "full_hybrid": RetrievalSettings(),
}


async def ablations(discovery, index, expected_adr, expected_code, provider=None):
    result, plans = {}, {}
    for name, settings in CONFIGURATIONS.items():
        plan = await HybridRetriever(index, embedder=provider).retrieve(
            discovery, settings
        )
        # Independent labels are consulted only after producing the plan.
        plans[name] = plan.model_dump(mode="json")
        result[name] = {
            "adr": scores(plan.ranked_adr, expected_adr),
            "code": scores(plan.ranked_code, expected_code),
            "candidate_counts": {
                "code": len(plan.ranked_code),
                "adr": len(plan.ranked_adr),
            },
            "duplicate_candidate_identities": len((*plan.ranked_code, *plan.ranked_adr))
            - len({c.canonical_id for c in (*plan.ranked_code, *plan.ranked_adr)}),
            "repeated_policy_statements": len(plan.requirements)
            - len({r.statement.casefold().strip() for r in plan.requirements}),
            "channels": [c.model_dump(mode="json") for c in plan.channels],
            "usage": plan.usage.model_dump(mode="json"),
            "limitations": dict(Counter(l.code for l in plan.limitations)),
            "compact_coverage_bytes": len(json.dumps(plan.compact_coverage).encode()),
            "audit_bytes": len(plan.model_dump_json().encode()),
            "top_adr": [
                {
                    "id": c.original_id,
                    "rank": c.rank,
                    "channels": sorted({h.channel for h in c.channels}),
                    "statement": next(
                        r.statement
                        for r in plan.requirements
                        if r.requirement_id == c.original_id
                    ),
                }
                for c in plan.ranked_adr[:10]
            ],
        }
    return result, plans


async def benchmark(tag, vector_snapshot=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe tag")
    destinations = [
        ROOT / f"A_{tag}_{suffix}.json"
        for suffix in ("index", "plans", "benchmark", "integrity")
    ]
    destinations.append(ROOT / f"independent_{tag}_benchmark.json")
    if any(path.exists() for path in destinations):
        raise FileExistsError("Use a fresh offline tag")
    protected = {path.name: file_hash(path) for path in ROOT.glob("*.json")}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    archive_path = ROOT / "claimiq_graph_snapshot.json"
    text = archive_path.read_text(encoding="utf-8")
    assert digest(text) == manifest["snapshot_hash"]
    archive = json.loads(text)
    changes = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(changes.repository_id, Path(case["source_root"]))
    frozen_vectors = (
        json.loads(vector_snapshot.read_text(encoding="utf-8"))
        if vector_snapshot
        else None
    )
    graph = ArchiveIndexGraph(archive, frozen_vectors)
    discovery = PreliminaryDiscovery.model_validate_json(
        (ROOT / "A_step2a_complete_discovery.json").read_text(encoding="utf-8")
    )
    repeated = await ArchitectureContextBuilder(
        graph,
        source,
        FixtureReadiness(
            changes.repository_id,
            changes.base_sha,
            manifest["snapshot_hash"],
            changes.github_repository_id,
            changes.base_branch,
        ),
        lambda _: asyncio.Lock(),
    ).discover(changes)
    assert repeated == discovery, (
        "Shared source-validator extraction altered historical discovery"
    )
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    start = time.monotonic()
    index = await DerivedIndexBuilder(
        graph, source, authority, mode="ARCHIVED"
    ).refresh(discovery.baseline)
    refresh_seconds = time.monotonic() - start
    # Labels originate in the previous source-grounded root-cause/context audit;
    # they are comparison-only and never passed to index/retrieval constructors.
    expected_adr = {"b9652205302af1fa53bddf46fcaa7c7fa0aecb07138504891a105f2ead97a31a"}
    expected_code = {
        "03febb1a3cf2695b052b97ee2cdce5e48a64e4735a71e0d00f6367b7d54ec336",
        "9987ba6f562016773fe92a1a5b4120cc654fd58d0660dfc016569a72cf58263e",
        "b97a6f8f1a1815922c65a9e59cc8f9a09d0a70ba0acef90b63720b6fa2023a9d",
    }
    results, plans = await ablations(discovery, index, expected_adr, expected_code)
    encoding = tiktoken.get_encoding("cl100k_base")
    count = lambda value: len(encoding.encode(value, disallowed_special=()))
    adr_units = [u for u in index.units if u.category == "ADR"]
    semantic_queries = [q for q in discovery.queries if q.channel == "ADR_SEMANTIC"]
    measured = {
        "live": False,
        "azure_calls": 0,
        "semantic_quality": "UNMEASURED",
        "source_scope": "full archived RKG; unchanged original revisions and Step 2A discovery",
        "archive_sha256": file_hash(archive_path),
        "discovery_unchanged": True,
        "fixture_readiness_only": True,
        "index_id": index.index_id,
        "index_refresh_seconds": refresh_seconds,
        "inventory_counts": {
            "code_rows": len(archive["chunks"]),
            "requirements": len(archive["requirements"]),
            "documents": len(archive["catalog"]),
        },
        "verified_index_counts": {
            "code": len(index.units) - len(adr_units),
            "adr": len(adr_units),
            "evidence": len(index.evidence),
            "existing_adr_chunk_vectors": len(index.adr_chunks),
        },
        "index_bytes": len(index.model_dump_json().encode()),
        "index_limitations": dict(Counter(l.code for l in index.limitations)),
        "queries": [
            {
                "channel": q.channel,
                "characters": len(q.text),
                "estimated_embedding_tokens": count(q.text),
            }
            for q in discovery.queries
        ],
        "ablations": results,
        "minimal_optional_real_embedding_plan": {
            "approval_required": True,
            "model": "text-embedding-3-large",
            "dimensions": 3072,
            "new_requirement_vectors": 0,
            "existing_chunk_vectors": len(index.adr_chunks),
            "query_vectors": len(semantic_queries),
            "index_input_tokens_cl100k": 0,
            "query_input_tokens_cl100k": sum(count(q.text) for q in semantic_queries),
            "index_calls_at_batch_64": 0,
            "query_calls_at_batch_8": (len(semantic_queries) + 7) // 8,
            "retries": 0,
            "generation_calls": 0,
        },
    }
    independent_discovery, independent_index, _, _ = await prepared(embedding=runner())
    independent_results, independent_plans = await ablations(
        independent_discovery,
        independent_index,
        {"governing", "global"},
        {"chunk-service", "task", "caller"},
        runner(),
    )
    independent = {
        "repository": independent_discovery.revision.repository_id,
        "fixture": "synthetic request/Task/caller workflow; independent of ClaimIQ",
        "semantic_quality": "MOCK_ONLY_NOT_PROVIDER_PERFORMANCE",
        "azure_calls": 0,
        "ablations": independent_results,
        "plans": independent_plans,
    }
    original_sources = {
        e.evidence_id: e for e in (*discovery.evidence, *index.evidence)
    }
    for payload in plans.values():
        for evidence in payload["evidence"]:
            assert evidence == original_sources[evidence["evidence_id"]].model_dump(
                mode="json"
            )
    unchanged = {
        name: file_hash(ROOT / name) == previous for name, previous in protected.items()
    }
    assert all(unchanged.values())
    save(destinations[0], index.model_dump(mode="json"))
    save(destinations[1], plans)
    save(destinations[2], measured)
    save(
        destinations[3],
        {
            "protected_artifacts": unchanged,
            "historical_data_unchanged": True,
            "ledger_unchanged": True,
            "source_identity_preserved": True,
            "azure_calls": 0,
        },
    )
    save(destinations[4], independent)
    print(
        json.dumps(
            {
                "artifacts": [str(path) for path in destinations],
                "verified_index_counts": measured["verified_index_counts"],
                "index_refresh_seconds": refresh_seconds,
                "ablations": {
                    name: {
                        key: value
                        for key, value in result.items()
                        if key in {"adr", "code", "candidate_counts", "usage"}
                    }
                    for name, result in results.items()
                },
                "minimal_optional_real_embedding_plan": measured[
                    "minimal_optional_real_embedding_plan"
                ],
                "historical_data_unchanged": True,
                "azure_calls": 0,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="step2b")
    parser.add_argument("--chunk-vector-snapshot", type=Path)
    args = parser.parse_args()
    asyncio.run(benchmark(args.tag, args.chunk_vector_snapshot))


if __name__ == "__main__":
    main()
