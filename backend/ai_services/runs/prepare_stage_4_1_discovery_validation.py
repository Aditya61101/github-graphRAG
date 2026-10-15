"""Offline Step 2A replay of full archived RKG and unchanged Case A sources."""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from ai_services.decision_guard import ArchitectureContextBuilder, PreliminaryDiscovery
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


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


async def prepare(tag):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
        raise ValueError("Unsafe artifact tag")
    paths = [
        ROOT / f"A_{tag}_{suffix}.json"
        for suffix in ("discovery", "summary", "integrity")
    ]
    if any(p.exists() for p in paths):
        raise FileExistsError("Use a new offline artifact tag")
    protected = {p.name: file_hash(p) for p in ROOT.glob("*.json")}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    graph_path = ROOT / "claimiq_graph_snapshot.json"
    archive_text = graph_path.read_text(encoding="utf-8")
    assert digest(archive_text) == manifest["snapshot_hash"]
    archive = json.loads(archive_text)
    changes = PullRequestChangeSet.model_validate_json(
        (ROOT / "A_changes.json").read_text(encoding="utf-8")
    )
    case = next(c for c in manifest["cases"] if c["id"] == "A")
    source, _ = source_for(changes.repository_id, Path(case["source_root"]))
    discovery = await ArchitectureContextBuilder(
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
    ).discover(changes)
    assert (
        PreliminaryDiscovery.model_validate_json(discovery.model_dump_json())
        == discovery
    )
    summary = {
        "live": False,
        "azure_calls": 0,
        "final_context_constructed": False,
        "archive_sha256": file_hash(graph_path),
        "fixture_readiness_only": True,
        "repository_id": discovery.revision.repository_id,
        "revision": discovery.revision.model_dump(mode="json"),
        "baseline": discovery.baseline.model_dump(mode="json"),
        "seeds": [
            {
                "seed_id": s.seed_id,
                "path": s.path,
                "symbols": s.changed_symbols,
                "mapping": s.mapping,
                "source_count": len(s.evidence_ids),
            }
            for s in discovery.seeds
        ],
        "entities": [
            {"id": e.entity_id, "name": e.name, "paths": e.paths, "origin": e.origin}
            for e in discovery.entities
        ],
        "relationships": [
            {
                "source": r.source_id,
                "type": r.relationship_type,
                "target": r.target_id,
                "provenance": r.provenance,
            }
            for r in discovery.relationships
        ],
        "source_paths": sorted({e.path for e in discovery.evidence if e.path}),
        "evidence_count": len(discovery.evidence),
        "queries": [
            {
                "channel": q.channel,
                "characters": len(q.text),
                "text": q.text,
                "seed_ids": q.seed_ids,
                "source_evidence_ids": q.source_evidence_ids,
            }
            for q in discovery.queries
        ],
        "limitations": dict(Counter(l.code for l in discovery.limitations)),
    }
    unchanged = {
        name: file_hash(ROOT / name) == value for name, value in protected.items()
    }
    assert all(unchanged.values())
    save(paths[0], discovery.model_dump(mode="json"))
    save(paths[1], summary)
    save(
        paths[2],
        {
            "protected_artifacts": unchanged,
            "historical_data_unchanged": True,
            "exact_contract_roundtrip": True,
            "azure_calls": 0,
        },
    )
    # The full query text and original sources are preserved in separate artifacts.
    print(
        json.dumps(
            {
                **summary,
                "queries": [{**q, "text": q["text"][:700]} for q in summary["queries"]],
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="step2a")
    asyncio.run(prepare(parser.parse_args().tag))


if __name__ == "__main__":
    main()
