"""
The ingestion pipeline, stage by stage. The same function handles a new repo
(Flow 1) and every later update (Flow 2): unchanged files, functions and
sections are skipped at every stage.

    0-1 discover     git ls-tree + rules           no LLM
    2-3 sync         parse + write + prune         no LLM
    4   resolve      IMPORTS/CALLS/INHERITS        no LLM
    5   enrich       function summaries            LLM, cached
    6   embed        functions + sections          embedding model
    7   bridge       docs <-> code                 (optional LLM check)
    8   modules      module summaries + product docs
    --  mark ready   commit pointer moves LAST
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .bridge import bridge
from .config import Settings
from .discover import discover, head_commit
from .embed import embed_pending
from .enrich import enrich_functions
from .modules import build_modules
from .resolve import resolve_links
from .store import GraphStore
from .sync import mark_repo_ready, sync_repo

log = logging.getLogger(__name__)


async def ingest(store: GraphStore, llm, embedder, root: Path, repo: str,
                 settings: Settings | None = None, verify_links: bool = False) -> dict:
    settings = settings or Settings()
    root = Path(root).resolve()
    report: dict = {"repo": repo}

    def stage(name: str, started: float, stats: dict) -> None:
        stats = dict(stats, seconds=round(time.perf_counter() - started, 2))
        report[name] = stats
        log.info("%-9s %s", name, stats)

    store.ensure_schema()
    previous = store.run("MATCH (r:Repo {name: $repo}) RETURN r.indexed_commit AS c", repo=repo)
    first_run = not previous or previous[0]["c"] is None
    report["mode"] = "new repo" if first_run else "update"

    t = time.perf_counter()
    files = discover(root)
    commit = head_commit(root)
    stage("discover", t, {"files": len(files), "commit": commit})

    t = time.perf_counter()
    synced = sync_repo(store, repo, root, files, commit)
    stage("sync", t, synced.summary())

    t = time.perf_counter()
    changed = None if first_run else synced.changed_code_files
    stage("resolve", t, resolve_links(store, repo, changed))

    t = time.perf_counter()
    stage("enrich", t, await enrich_functions(store, llm, repo, settings.llm_concurrency))

    t = time.perf_counter()
    stage("embed", t, embed_pending(store, embedder, repo))

    t = time.perf_counter()
    stage("bridge", t, await bridge(
        store, repo, top_k=settings.bridge_top_k, min_score=settings.bridge_min_score,
        llm=llm if verify_links else None, reverse=not first_run))

    t = time.perf_counter()
    stage("modules", t, await build_modules(
        store, llm, embedder, repo, top_k=2, min_score=settings.bridge_min_score))

    mark_repo_ready(store, repo, commit)
    report["status"] = "ready"
    return report
