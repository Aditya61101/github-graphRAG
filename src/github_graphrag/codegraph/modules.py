"""
Stage 8 - Modules: big-picture nodes for architecture and product questions.

    File   -[:IN_MODULE]->   Module      (one Module per source directory)
    Module -[:DEPENDS_ON {calls}]-> Module  (aggregated from CALLS)
    Section -[:RELATES_TO {score, method:'module'}]-> Module   (product docs only)

Each Module gets an LLM summary written from its functions' summaries, then
an embedding. A module is only re-summarised when its members' summaries
change (members_hash), so this is cheap to run on every sync.

Product docs (slides / Word) rarely name code, so they are linked here at the
module level, where their business language matches best.

Note: this uses directories instead of graph community detection (Leiden),
because Neo4j Aura Free has no Graph Data Science plugin. Directories are
also easier to explain in a demo.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import PurePosixPath

from .config import model_id
from .embed import embed_query
from .store import GraphStore

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_MEMBERS_IN_PROMPT = 40

MODULE_PROMPT = """You are describing one module of a software system for new engineers
and product managers.

Module: {name}
Functions in this module (name: summary):
{members}

In 2-3 plain-English sentences, describe what this module is responsible for and
which business capability it supports. Return only the sentences."""


def _module_path(file_path: str) -> str:
    parent = str(PurePosixPath(file_path).parent)
    return "(root)" if parent == "." else parent


async def build_modules(store: GraphStore, llm, embedder, repo: str,
                        top_k: int = 2, min_score: float = 0.75) -> dict:
    # ------------------------------------------------------------ membership
    files = store.run("MATCH (f:File {repo: $repo}) RETURN f.id AS id, f.path AS path",
                      repo=repo)
    rows = [{"file_id": f["id"], "module_id": f"{repo}:{_module_path(f['path'])}",
             "name": _module_path(f["path"]), "repo": repo} for f in files]
    store.run("MATCH (:File {repo: $repo})-[r:IN_MODULE]->() DELETE r", repo=repo)
    store.run_batched(
        """
        UNWIND $rows AS row
        MERGE (m:Module {id: row.module_id})
        SET m.repo = row.repo, m.name = row.name
        WITH m, row
        MATCH (f:File {id: row.file_id})
        MERGE (f)-[:IN_MODULE]->(m)
        """, rows)
    store.run(
        "MATCH (m:Module {repo: $repo}) WHERE NOT ()-[:IN_MODULE]->(m) DETACH DELETE m",
        repo=repo)

    # ------------------------------------------------------------ dependencies
    store.run("MATCH (:Module {repo: $repo})-[r:DEPENDS_ON]->() DELETE r", repo=repo)
    store.run(
        """
        MATCH (m1:Module {repo: $repo})<-[:IN_MODULE]-(:File)-[:DEFINES]->(:Function)
              -[:CALLS]->(:Function)<-[:DEFINES]-(:File)-[:IN_MODULE]->(m2:Module)
        WHERE m1 <> m2
        WITH m1, m2, count(*) AS calls
        MERGE (m1)-[d:DEPENDS_ON]->(m2)
        SET d.calls = calls
        """, repo=repo)

    # ------------------------------------------------------------ summaries
    members = store.run(
        """
        MATCH (m:Module {repo: $repo})<-[:IN_MODULE]-(:File)-[:DEFINES]->(f:Function)
        WHERE f.summary IS NOT NULL
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
        WITH m, f, count(caller) AS used
        ORDER BY m.id, f.is_test ASC, used DESC, f.qname
        RETURN m.id AS id, m.name AS name, m.members_hash AS old_hash,
               collect(f.qname + ': ' + f.summary) AS members,
               collect(f.summary_hash) AS hashes
        """, repo=repo)

    to_summarise = []
    for m in members:
        new_hash = hashlib.sha256("|".join(sorted(m["hashes"])).encode()).hexdigest()
        if new_hash != m["old_hash"]:
            to_summarise.append((m, new_hash))

    model = model_id(llm)
    results = await asyncio.gather(*(_summarise(store, llm, model, m, h)
                                     for m, h in to_summarise))
    updated = [r for r in results if r]
    if updated:
        vectors = [embed_query(embedder, f"module: {r['name']}\nsummary: {r['summary']}")
                   for r in updated]
        store.ensure_vector_indexes(len(vectors[0]), model_id(embedder))
        store.run_batched(
            """
            UNWIND $rows AS row
            MATCH (m:Module {id: row.id})
            SET m.summary = row.summary, m.members_hash = row.members_hash,
                m.embedding = row.vector
            """, [dict(r, vector=v) for r, v in zip(updated, vectors)])

    # ------------------------------------------------------------ product docs
    linked = await _link_product_docs(store, repo, top_k, min_score,
                                      modules_changed=bool(updated))
    return {"modules": len(members), "summarised": len(updated), "product_links": linked}


async def _summarise(store, llm, model, m, members_hash) -> dict | None:
    key = hashlib.sha256(f"module|{members_hash}|{model}|{PROMPT_VERSION}".encode()).hexdigest()
    cached = store.run("MATCH (c:SummaryCache {key: $key}) RETURN c.summary AS s", key=key)
    if cached:
        summary = cached[0]["s"]
    else:
        prompt = MODULE_PROMPT.format(
            name=m["name"],
            members="\n".join(f"- {x}" for x in m["members"][:MAX_MEMBERS_IN_PROMPT]))
        try:
            summary = " ".join(((await llm.ainvoke(prompt)).content or "").split())
        except Exception as err:
            log.warning("module summary failed for %s: %s", m["id"], err)
            return None
        if not summary:
            return None
        store.run("MERGE (c:SummaryCache {key: $key}) SET c.summary = $s", key=key, s=summary)
    return {"id": m["id"], "name": m["name"], "summary": summary, "members_hash": members_hash}


async def _link_product_docs(store, repo, top_k, min_score, modules_changed: bool) -> int:
    condition = "" if modules_changed else \
        "AND (s.module_link_hash IS NULL OR s.module_link_hash <> s.content_hash)"
    sections = store.run(
        f"""
        MATCH (s:Section {{repo: $repo}})
        WHERE s.doc_type = 'product' AND s.embedding IS NOT NULL {condition}
        RETURN s.id AS id, s.embedding AS embedding
        """, repo=repo)
    if not sections:
        return 0

    rows = []
    for s in sections:
        for hit in store.vector_search("Module", s["embedding"], top_k, repo):
            if hit["score"] >= min_score:
                rows.append({"sid": s["id"], "mid": hit["id"], "score": hit["score"]})

    store.run(
        """
        UNWIND $ids AS sid
        MATCH (s:Section {id: sid})-[r:RELATES_TO]->(:Module)
        DELETE r
        """, ids=[s["id"] for s in sections])
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (s:Section {id: row.sid}), (m:Module {id: row.mid})
        MERGE (s)-[r:RELATES_TO]->(m)
        SET r.score = row.score, r.method = 'module'
        """, rows)
    store.run(
        """
        UNWIND $ids AS sid
        MATCH (s:Section {id: sid}) SET s.module_link_hash = s.content_hash
        """, ids=[s["id"] for s in sections])
    return len(rows)
