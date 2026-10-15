"""Step 2C offline full-archive context/packet comparison. No model client."""

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import tiktoken

from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.hybrid import (
    EmbeddingIdentity,
    EmbeddingRunner,
    HybridContextRetrieval,
    HybridRetriever,
    RetrievalSettings,
)
from ai_services.decision_guard.hybrid.models import (
    SearchIndex,
    VectorCompatibilityManifest,
    fingerprint,
    vector_key,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.decision_guard.settings import ContextSettings
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import (
    ArchiveIndexGraph,
    file_hash,
    save,
)
from ai_services.runs.prepare_stage_4_2_live_validation import (
    ROOT,
    FixtureReadiness,
    source_for,
)
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore


class ArchiveContextGraph(ArchiveIndexGraph):
    def code_chunks(self, chunk_ids, limit):
        return self.page(
            [r for r in self.payload["chunks"] if r["chunk"]["id"] in chunk_ids], limit
        )


def hash_vector(text):
    data = hashlib.sha256(text.encode()).digest()
    return [1 + data[0] / 255, 1 + data[1] / 255]


class ControlledMock:
    dimensions = 2

    async def embed(self, texts):
        return [hash_vector(t) for t in texts]


def mock_index(index):
    """Separate synthetic geometry; preserved original vectors are never changed."""
    identity = EmbeddingIdentity(
        provider="controlled-mock",
        model="sha256-fixture",
        dimensions=2,
        revision="integration-v1",
        provenance="MOCK",
    )
    manifest = VectorCompatibilityManifest(
        configuration_id="step2c-mock-v1",
        identity=identity,
        confirmation="MOCK_FIXTURE",
        confirmation_source="Offline hash vectors exercise integration only; not semantic quality",
    )
    data = index.model_dump(mode="json")
    data["vector_manifest"] = manifest.model_dump(mode="json")
    evidence = {e.evidence_id: e for e in index.evidence}
    for c in data["adr_chunks"]:
        c["vector"] = {
            "identity": identity.model_dump(mode="json"),
            "key": vector_key(SimpleNamespace(**c), identity),
            "values": hash_vector(evidence[c["evidence_id"]].excerpt),
        }
        c.update(
            metadata_basis="MOCK_FIXTURE",
            observed_metadata_json="{}",
            verified_metadata_fields=[],
        )
    data["index_id"] = fingerprint(
        {
            k: v
            for k, v in data.items()
            if k not in {"index_id", "embedding_requests", "embeddings_reused"}
        }
    )
    return SearchIndex.model_validate_json(json.dumps(data)), identity


async def benchmark(tag, index_path):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe tag")
    if list(ROOT.glob(f"A_{tag}_*.json")):
        raise FileExistsError("Use a fresh offline tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    archive_text = (ROOT / "claimiq_graph_snapshot.json").read_text(encoding="utf-8")
    assert digest(archive_text) == manifest["snapshot_hash"]
    archive = json.loads(archive_text)
    event = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    index = SearchIndex.model_validate_json(index_path.read_text(encoding="utf-8"))
    old = ArchitectureContext.model_validate_json(
        (ROOT / "A_context.json").read_text(encoding="utf-8")
    )
    expected = old.requirements[0].requirement_id  # post-build scoring only
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(event.repository_id, Path(case["source_root"]))
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    encoding = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(encoding.encode(t, disallowed_special=())),
    )
    evaluation_settings = EvaluationSettings.model_validate(manifest["settings"])
    context_settings = ContextSettings(max_decisions=4, max_constraints=8)
    synthetic, mock_identity = mock_index(index)
    configs = {
        "step1_baseline": None,
        "structural_only": RetrievalSettings(
            lexical=False, scope=False, semantic=False
        ),
        "structural_bm25_scope": RetrievalSettings(semantic=False),
        "full_semantic_unavailable": RetrievalSettings(),
        "full_controlled_mock": RetrievalSettings(),
    }
    metrics = {}
    for name, settings in configs.items():
        sizing = ContextRequestBudget(
            source, authority, settings=evaluation_settings, token_counter=counter
        )
        config = None
        if settings:
            is_mock = name == "full_controlled_mock"
            embedder = (
                EmbeddingRunner(ControlledMock(), mock_identity) if is_mock else None
            )

            def factory(snapshot, *, authority, embedder=embedder):
                return HybridRetriever(snapshot, authority=authority, embedder=embedder)

            config = HybridContextRetrieval(
                index=synthetic if is_mock else index,
                authority=authority,
                mode="ARCHIVED",
                settings=settings,
                retriever_factory=factory,
            )
        builder = ArchitectureContextBuilder(
            ArchiveContextGraph(archive),
            source,
            FixtureReadiness(
                event.repository_id,
                event.base_sha,
                manifest["snapshot_hash"],
                event.github_repository_id,
                event.base_branch,
            ),
            lambda _: asyncio.Lock(),
            settings=context_settings,
            token_counter=counter,
            request_budget=sizing,
            hybrid=config,
        )
        started = time.monotonic()
        ctx = validate_context(await builder.build(event))
        latency = time.monotonic() - started
        assert ctx.revision == old.revision
        assert {(a.adr_id, a.content_hash) for a in ctx.adr_snapshot} == {
            (a.adr_id, a.content_hash) for a in old.adr_snapshot
        }
        rule = next((r for r in ctx.requirements if r.requirement_id == expected), None)
        ev = {e.evidence_id: e for e in ctx.evidence}
        all_fit = all(sizing.fits(p, "ASSESS") for p in sizing.last_packets)
        assert (
            ctx.budget.estimated_tokens <= context_settings.max_context_tokens
            and all_fit
        )
        if config:
            assert builder.last_retrieval_plan is not None, builder.last_retrieval_audit
        packets = [
            {
                "unit_ids": [u.unit_id for u in p.units],
                "input_tokens": sizing.count(p, "ASSESS"),
                "reserved_output_tokens": evaluation_settings.max_output_tokens,
            }
            for p in sizing.last_packets
        ]
        metric = {
            "requirements": [
                {
                    "id": r.requirement_id,
                    "statement": r.statement,
                    "reasons": r.discovery_reasons,
                }
                for r in ctx.requirements
            ],
            "counts": {
                "requirements": len(ctx.requirements),
                "entities": len(ctx.entities),
                "relationships": len(ctx.relationships),
                "evidence": len(ctx.evidence),
            },
            "entities": [
                {"id": e.entity_id, "name": e.name, "paths": e.paths}
                for e in ctx.entities
            ],
            "relationships": [r.model_dump(mode="json") for r in ctx.relationships],
            "expected_rule_retained": bool(rule),
            "expected_rule_sources": [
                {
                    "chunk_id": ev[eid].chunk_id,
                    "excerpt": ev[eid].excerpt,
                    "qualification": eid in rule.scope_evidence_ids,
                }
                for eid in rule.evidence_ids
            ]
            if rule
            else [],
            "model_context_tokens": counter(model_context_json(ctx)),
            "audit_context_tokens": counter(ctx.model_dump_json()),
            "coverage": dict(Counter(l.code for l in ctx.coverage.limitations)),
            "context_latency_seconds": latency,
            "retrieval_latency_seconds": builder.last_retrieval_plan.usage.latency_seconds
            if builder.last_retrieval_plan
            else 0,
            "semantic_quality": "MOCK_INTEGRATION_ONLY"
            if name == "full_controlled_mock"
            else "UNMEASURED",
            "assessment_units": sum(len(p.units) for p in sizing.last_packets),
            "assessment_packets": len(packets),
            "packets": packets,
            "combined_assessment_reservation": sum(
                p["input_tokens"] + p["reserved_output_tokens"] for p in packets
            ),
            "all_initial_packets_fit": all_fit,
            "scope_group_integrity": all(
                set(r.scope_evidence_ids) <= set(r.evidence_ids)
                for r in ctx.requirements
            ),
        }
        metrics[name] = metric
        save(ROOT / f"A_{tag}_{name}_context.json", ctx.model_dump(mode="json"))
        if builder.last_retrieval_plan:
            save(
                ROOT / f"A_{tag}_{name}_plan.json",
                builder.last_retrieval_plan.model_dump(mode="json"),
            )
        save(
            ROOT / f"A_{tag}_{name}_audit.json",
            builder.last_retrieval_audit or {"retrieval": "DISABLED"},
        )
        save(
            ROOT / f"A_{tag}_{name}_packets.json",
            [p.model_dump(mode="json") for p in sizing.last_packets],
        )
        print(
            json.dumps(
                {
                    "configuration": name,
                    **{
                        k: metric[k]
                        for k in (
                            "counts",
                            "expected_rule_retained",
                            "model_context_tokens",
                            "assessment_units",
                            "assessment_packets",
                            "combined_assessment_reservation",
                            "context_latency_seconds",
                        )
                    },
                }
            ),
            flush=True,
        )
    preserved = {
        name: file_hash(ROOT / name) == value for name, value in protected.items()
    }
    assert all(preserved.values())
    save(
        ROOT / f"A_{tag}_comparison.json",
        {
            "offline": True,
            "real_model_calls": 0,
            "index_id": index.index_id,
            "fixture_readiness_only": True,
            "context_limit": context_settings.max_context_tokens,
            "input_limit": evaluation_settings.max_input_tokens,
            "output_reserve": evaluation_settings.max_output_tokens,
            "provider_window": evaluation_settings.model_window,
            "configurations": metrics,
        },
    )
    save(
        ROOT / f"A_{tag}_integrity.json",
        {"historical_files_unchanged": preserved, "azure_calls": 0, "live_writes": 0},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--index", type=Path, default=ROOT / "A_step2b_chunk_reuse_index.json"
    )
    args = parser.parse_args()
    asyncio.run(benchmark(args.tag, args.index))


if __name__ == "__main__":
    main()
