"""
Stage 6 - Embed: vectors for Functions (from their summary) and Sections.

We embed text that puts code and questions in the same "language":

    repo: claimIQ | file: backend/app/core/security.py
    symbol: verify_password (plain: str, hashed: str) -> bool
    summary: Checks a login password against the stored hash.

Only nodes flagged `needs_embedding` (or never embedded) are processed.
Every embedded node is flagged `needs_bridge` for stage 7.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .config import model_id
from .store import GraphStore

log = logging.getLogger(__name__)

MAX_SECTION_EMBED_CHARS = 6000


def function_text(row: dict) -> str:
    return (
        f"repo: {row['repo']} | file: {row['file']}\n"
        f"symbol: {row['qname']} {row.get('signature') or ''}\n"
        f"summary: {row['summary']}"
    )


def section_text(row: dict) -> str:
    return f"document: {row['path']} > {row['heading']}\n{row['text'][:MAX_SECTION_EMBED_CHARS]}"


def _embed_one(embedder, text: str, attempts: int = 5) -> list[float]:
    for attempt in range(1, attempts + 1):
        try:
            return embedder.embed_query(text)
        except Exception as err:  # rate limit / transient API error
            if attempt == attempts:
                raise
            wait = 2 ** attempt
            log.warning("embedding failed (%s); retrying in %ss", err, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _embed_all(embedder, texts: list[str], workers: int = 4) -> list[list[float]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: _embed_one(embedder, t), texts))


def embed_pending(store: GraphStore, embedder, repo: str) -> dict:
    functions = store.run(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary IS NOT NULL AND (f.needs_embedding = true OR f.embedding IS NULL)
        RETURN f.id AS id, f.repo AS repo, f.file AS file, f.qname AS qname,
               f.signature AS signature, f.summary AS summary
        """, repo=repo)
    sections = store.run(
        """
        MATCH (s:Section {repo: $repo})
        WHERE s.needs_embedding = true OR s.embedding IS NULL
        RETURN s.id AS id, s.path AS path, s.heading AS heading, s.text AS text
        """, repo=repo)

    if not functions and not sections:
        return {"functions": 0, "sections": 0}

    fn_vectors = _embed_all(embedder, [function_text(r) for r in functions])
    sec_vectors = _embed_all(embedder, [section_text(r) for r in sections])

    dims = len((fn_vectors or sec_vectors)[0])
    store.ensure_vector_indexes(dims, model_id(embedder))

    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (f:Function {id: row.id})
        SET f.embedding = row.vector, f.needs_embedding = false, f.needs_bridge = true
        """,
        [{"id": r["id"], "vector": v} for r, v in zip(functions, fn_vectors)], size=200)
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (s:Section {id: row.id})
        SET s.embedding = row.vector, s.needs_embedding = false, s.needs_bridge = true
        """,
        [{"id": r["id"], "vector": v} for r, v in zip(sections, sec_vectors)], size=200)

    return {"functions": len(functions), "sections": len(sections), "dims": dims}


def embed_query(embedder, text: str) -> list[float]:
    return _embed_one(embedder, text)
