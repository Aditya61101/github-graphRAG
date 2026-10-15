"""Complete offline preflight only. Never constructs an Azure client or calls a model."""

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

import tiktoken
from dotenv import load_dotenv

from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.discovery import PreliminaryDiscovery
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context
from ai_services.decision_guard.hybrid import (
    DerivedIndexBuilder,
    EmbeddingLimits,
    HybridContextRetrieval,
    RetrievalSettings,
)
from ai_services.decision_guard.hybrid.models import (
    SearchIndex,
    confirmed_legacy_manifest,
)
from ai_services.decision_guard.models import ArchitectureContext, digest
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


async def prepare(tag):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe tag")
    if list(ROOT.glob(f"A_{tag}_*.json")):
        raise FileExistsError("Use a fresh preflight tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    load_dotenv(".env")
    config = {
        "deployment": os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
        "dimensions": int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION"),
        "endpoint_configured": bool(os.getenv("AZURE_OPENAI_ENDPOINT")),
        "credential_configured": bool(os.getenv("AZURE_OPENAI_API_KEY")),
    }
    if (
        config["dimensions"] != 3072
        or not config["deployment"]
        or "text-embedding-3-large" not in config["deployment"]
        or not all(
            config[k]
            for k in ("api_version", "endpoint_configured", "credential_configured")
        )
    ):
        raise ValueError(
            "Configured embedding identity/credentials cannot be verified offline"
        )
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    archive_text = (ROOT / "claimiq_graph_snapshot.json").read_text(encoding="utf-8")
    assert digest(archive_text) == manifest["snapshot_hash"]
    archive = json.loads(archive_text)
    event = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(event.repository_id, Path(case["source_root"]))
    vectors = json.loads(
        (ROOT / "adr_existing_chunk_vectors_v1.json").read_text(encoding="utf-8")
    )
    graph = ArchiveContextGraph(archive, vectors)
    authority = UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
    readiness = FixtureReadiness(
        event.repository_id,
        event.base_sha,
        manifest["snapshot_hash"],
        event.github_repository_id,
        event.base_branch,
    )
    discovery = await ArchitectureContextBuilder(
        graph, source, readiness, lambda _: asyncio.Lock()
    ).discover(event)
    archived_discovery = PreliminaryDiscovery.model_validate_json(
        (ROOT / "A_step2a_complete_discovery.json").read_text(encoding="utf-8")
    )
    assert discovery == archived_discovery
    queries = [q for q in discovery.queries if q.channel == "ADR_SEMANTIC"]
    assert len(queries) == 1
    query = queries[0]
    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = len(encoding.encode(query.text, disallowed_special=()))
    compatibility = confirmed_legacy_manifest()
    assert compatibility.identity.dimensions == config["dimensions"]
    started = time.perf_counter()
    index = await DerivedIndexBuilder(
        graph, source, authority, mode="ARCHIVED", compatibility_manifest=compatibility
    ).refresh(discovery.baseline)
    refresh_seconds = time.perf_counter() - started
    preserved_index = SearchIndex.model_validate_json(
        (ROOT / "A_step2b_chunk_reuse_index.json").read_text(encoding="utf-8")
    )
    assert index == preserved_index
    assert len(index.adr_chunks) == 21 and index.embedding_requests == 0
    existing = {r["chunk"]["id"]: r for r in vectors["rows"]}
    ev = {e.evidence_id: e for e in index.evidence}
    original_primary = {}
    for row in archive["requirements"]:
        for support in row["support"]:
            original_primary.setdefault(support["chunk"]["id"], set()).add(
                row["knowledge"]["id"]
            )
    checks = []
    for c in index.adr_chunks:
        raw = existing[c.chunk_id]
        assert c.vector.values == tuple(raw["chunk"]["embedding"])
        assert (
            c.repository_id == event.repository_id
            and raw["adr"]["repository"] == event.repository_id
        )
        assert c.document_hash == raw["adr"][
            "content_hash"
        ] and c.content_hash == digest(raw["chunk"]["text"])
        assert set(c.requirement_ids) == original_primary.get(c.chunk_id, set())
        assert (
            c.metadata_basis == "LEGACY_CONFIRMED" and c.verified_metadata_fields == ()
        )
        checks.append(
            {
                "adr_id": c.adr_id,
                "chunk_id": c.chunk_id,
                "section": ev[c.evidence_id].section,
                "requirements": len(c.requirement_ids),
                "vector_hash": digest(json.dumps(c.vector.values)),
                "original_source_verified": True,
                "original_vector_unchanged": True,
                "provenance": c.metadata_basis,
            }
        )
    settings = EvaluationSettings.model_validate(manifest["settings"])
    model_encoding = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(model_encoding.encode(t, disallowed_special=())),
    )
    sizing = ContextRequestBudget(
        source, authority, settings=settings, token_counter=counter
    )
    builder = ArchitectureContextBuilder(
        graph,
        source,
        readiness,
        lambda _: asyncio.Lock(),
        settings=ContextSettings(max_decisions=4, max_constraints=8),
        token_counter=counter,
        request_budget=sizing,
        hybrid=HybridContextRetrieval(
            index=index,
            authority=authority,
            mode="ARCHIVED",
            settings=RetrievalSettings(semantic=False),
        ),
    )
    context = validate_context(await builder.build(event))
    plan = builder.last_retrieval_plan
    assert plan is not None
    # Only after retrieval/selection: validation labels never reach the pipeline.
    original = ArchitectureContext.model_validate_json(
        (ROOT / "A_context.json").read_text(encoding="utf-8")
    )
    expected = original.requirements[0].requirement_id
    candidate = next(c for c in plan.ranked_adr if c.original_id == expected)
    selected = next(r for r in context.requirements if r.requirement_id == expected)
    receipts = authority.receipts(context)
    assert len(receipts) == 3 and all(r.status == "APPROVED" for r in receipts)
    packet_inputs = [sizing.count(p, "ASSESS") for p in sizing.last_packets]
    limits = EmbeddingLimits(
        max_requests=1,
        batch_size=1,
        concurrency=1,
        retries=0,
        authorized_billable=False,
    )
    result = {
        "status": "OFFLINE_PREFLIGHT_COMPLETE_AWAITING_EXPLICIT_EMBEDDING_APPROVAL",
        "provider_calls": 0,
        "generative_calls": 0,
        "fixture_readiness_only": True,
        "revision": discovery.revision.model_dump(mode="json"),
        "archive_byte_sha256": file_hash(ROOT / "claimiq_graph_snapshot.json"),
        "query_id": query.query_id,
        "query_hash": digest(query.text),
        "query_characters": len(query.text),
        "query_input_tokens": tokens,
        "query_estimator": "cl100k_base",
        "query_unchanged_from_preserved_step2a": True,
        "query_source_ids": query.source_evidence_ids,
        "query_seed_ids": query.seed_ids,
        "configuration": config,
        "vector_manifest": compatibility.model_dump(mode="json"),
        "index_id": index.index_id,
        "index_refresh_seconds": refresh_seconds,
        "verified_chunks": checks,
        "existing_vectors": len(checks),
        "requirements_mapped": len(
            {rid for c in index.adr_chunks for rid in c.requirement_ids}
        ),
        "source_only_chunks": sum(not c.requirement_ids for c in index.adr_chunks),
        "index_reembedding_requests": index.embedding_requests,
        "requested_budget": {
            **asdict(limits),
            "query_count": 1,
            "input_tokens": tokens,
            "cost_formula_usd": f"{tokens}/1000000 * Azure account USD rate per million input tokens",
            "illustrative_rate_usd_per_million": 0.13,
            "illustrative_cost_usd": tokens * 0.13 / 1000000,
            "azure_account_rate_verified": False,
        },
        "baseline": {
            "governing_requirement_rank": candidate.rank,
            "code_candidates": len(plan.ranked_code),
            "adr_candidates": len(plan.ranked_adr),
            "requirements_retained": len(context.requirements),
            "entities_retained": len(context.entities),
            "relationships_retained": len(context.relationships),
            "model_context_tokens": counter(model_context_json(context)),
            "units": sum(len(p.units) for p in sizing.last_packets),
            "packets": len(sizing.last_packets),
            "input_tokens": packet_inputs,
            "assessment_reservation": sum(packet_inputs)
            + len(packet_inputs) * settings.max_output_tokens,
            "governing_original_sources": [
                {"chunk_id": ev[eid].chunk_id, "content_hash": ev[eid].content_hash}
                for eid in selected.evidence_ids
            ],
        },
        "real_semantic_ranks": None,
        "semantic_enabled_comparison": "NOT_EXECUTED_APPROVAL_REQUIRED",
    }
    assert all(file_hash(ROOT / name) == sha for name, sha in protected.items())
    save(ROOT / f"A_{tag}_query.json", query.model_dump(mode="json"))
    save(ROOT / f"A_{tag}_preflight.json", result)
    save(ROOT / f"A_{tag}_baseline_plan.json", plan.model_dump(mode="json"))
    save(ROOT / f"A_{tag}_baseline_context.json", context.model_dump(mode="json"))
    save(
        ROOT / f"A_{tag}_baseline_packets.json",
        [p.model_dump(mode="json") for p in sizing.last_packets],
    )
    save(
        ROOT / f"A_{tag}_integrity.json",
        {
            "historical_files_unchanged": {name: True for name in protected},
            "ledger_sha256": file_hash(ROOT / "aggregate_usage.json"),
            "billable_calls": 0,
        },
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "query_hash",
                    "query_characters",
                    "query_input_tokens",
                    "existing_vectors",
                    "requirements_mapped",
                    "index_refresh_seconds",
                    "baseline",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    asyncio.run(prepare(parser.parse_args().tag))
