"""
Stage 7 - Bridge: link document Sections to code.

    Section -[:MENTIONS {confidence: 1.0}]->        Function | Class | Endpoint
        the section names the code exactly (`verify_password`, UserRepository,
        POST /auth/login)

    Section -[:RELATES_TO {score, method:'vector'}]-> Function
        no exact name, but the section's meaning matches the function's summary
        (top-k per section, above BRIDGE_MIN_SCORE)

Forward pass: every Section flagged `needs_bridge` (new or edited docs).
Reverse pass (updates only): every Function flagged `needs_bridge` (new or
changed code) is matched against existing sections, so an unchanged doc can
still pick up a link to new code.

Product docs (slides / Word) are also linked to Modules in stage 8.
"""

from __future__ import annotations

import logging
import re

from .resolve import COMMON_NAMES
from .store import GraphStore

log = logging.getLogger(__name__)

VERIFY_PROMPT = """Does this documentation passage describe the code below?
Answer only "yes" or "no".

PASSAGE ({heading}):
{text}

CODE ({qname} in {file}):
{summary}
"""


def _targets(store: GraphStore, repo: str) -> list[dict]:
    """Things a doc can mention by name."""
    rows = []
    for label in ("Function", "Class"):
        rows += store.run(
            f"MATCH (n:{label} {{repo: $repo}}) "
            f"RETURN n.id AS id, n.name AS name, n.qname AS qname, '{label}' AS label",
            repo=repo)
    rows += store.run(
        """
        MATCH (e:Endpoint {repo: $repo})
        RETURN e.id AS id, e.route AS name, e.method + ' ' + e.route AS qname,
               'Endpoint' AS label
        """, repo=repo)
    return rows


def _is_distinctive(name: str) -> bool:
    """Can a plain-word match of this name be trusted?"""
    if name.lower() in COMMON_NAMES or len(name) < 5:
        return False
    return "_" in name or bool(re.search(r"[a-z][A-Z]", name)) or name[:1].isupper()


def find_mentions(text: str, targets: list[dict]) -> list[tuple[str, str]]:
    """(label, node id) of every target named in `text`."""
    found = []
    code_spans = set(re.findall(r"`([^`]+)`", text))
    for t in targets:
        name, qname = t["name"], t["qname"]
        if not name:
            continue
        if t["label"] == "Endpoint":
            if qname in text or f"`{name}`" in text or any(name == s.strip() for s in code_spans):
                found.append((t["label"], t["id"]))
            continue
        in_code = any(re.fullmatch(rf"(\w+\.)*{re.escape(name)}(\(.*\))?", s.strip())
                      for s in code_spans)
        qualified = "." in qname and re.search(rf"\b{re.escape(qname)}\b", text)
        plain = _is_distinctive(name) and re.search(rf"\b{re.escape(name)}\b", text)
        if in_code or qualified or plain:
            found.append((t["label"], t["id"]))
    return found


async def bridge(store: GraphStore, repo: str, top_k: int = 3, min_score: float = 0.75,
                 llm=None, reverse: bool = True) -> dict:
    """llm: pass one to double-check every vector link with a yes/no question."""
    targets = _targets(store, repo)
    stats = {"sections": 0, "mentions": 0, "vector_links": 0, "rejected_by_llm": 0,
             "reverse_functions": 0}

    sections = store.run(
        """
        MATCH (s:Section {repo: $repo}) WHERE s.needs_bridge = true
        RETURN s.id AS id, s.heading AS heading, s.text AS text, s.embedding AS embedding
        """, repo=repo)
    stats["sections"] = len(sections)

    mention_rows, vector_rows = [], []
    for s in sections:
        mentioned = find_mentions(s["text"], targets)
        mention_rows += [{"sid": s["id"], "label": lbl, "tid": t} for lbl, t in mentioned]
        mentioned_ids = {t for _, t in mentioned}
        if s["embedding"]:
            for hit in store.vector_search("Function", s["embedding"], top_k, repo):
                if hit["score"] >= min_score and hit["id"] not in mentioned_ids:
                    vector_rows.append({"sid": s["id"], "fid": hit["id"], "score": hit["score"]})

    # Reverse pass: new/changed functions against unchanged sections
    if reverse:
        functions = store.run(
            """
            MATCH (f:Function {repo: $repo}) WHERE f.needs_bridge = true
            RETURN f.id AS id, f.name AS name, f.qname AS qname, f.embedding AS embedding
            """, repo=repo)
        stats["reverse_functions"] = len(functions)
        if functions:
            fresh = {s["id"] for s in sections}
            all_sections = [s for s in store.run(
                "MATCH (s:Section {repo: $repo}) RETURN s.id AS id, s.text AS text",
                repo=repo) if s["id"] not in fresh]
            fn_targets = [dict(f, label="Function") for f in functions]
            for s in all_sections:
                mention_rows += [{"sid": s["id"], "label": lbl, "tid": t}
                                 for lbl, t in find_mentions(s["text"], fn_targets)]
            for f in functions:
                if not f["embedding"]:
                    continue
                for hit in store.vector_search("Section", f["embedding"], top_k, repo):
                    if hit["score"] >= min_score and hit["id"] not in fresh:
                        vector_rows.append({"sid": hit["id"], "fid": f["id"],
                                            "score": hit["score"]})

    if llm is not None and vector_rows:
        vector_rows, rejected = await _verify(store, llm, vector_rows)
        stats["rejected_by_llm"] = rejected

    for label in ("Function", "Class", "Endpoint"):
        store.run_batched(
            f"""
            UNWIND $rows AS row
            MATCH (s:Section {{id: row.sid}}), (t:{label} {{id: row.tid}})
            MERGE (s)-[r:MENTIONS]->(t)
            SET r.confidence = 1.0
            """, [r for r in mention_rows if r["label"] == label])
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (s:Section {id: row.sid}), (f:Function {id: row.fid})
        WHERE NOT (s)-[:MENTIONS]->(f)
        MERGE (s)-[r:RELATES_TO]->(f)
        SET r.score = row.score, r.method = 'vector'
        """, vector_rows)
    for label in ("Section", "Function"):
        store.run(
            f"MATCH (n:{label} {{repo: $repo}}) WHERE n.needs_bridge = true "
            "SET n.needs_bridge = false", repo=repo)

    stats["mentions"] = len(mention_rows)
    stats["vector_links"] = len(vector_rows)
    return stats


async def _verify(store: GraphStore, llm, rows: list[dict]) -> tuple[list[dict], int]:
    import asyncio

    details = {
        (r["sid"], r["fid"]): r
        for r in store.run(
            """
            UNWIND $rows AS row
            MATCH (s:Section {id: row.sid}), (f:Function {id: row.fid})
            RETURN row.sid AS sid, row.fid AS fid, s.heading AS heading, s.text AS text,
                   f.qname AS qname, f.file AS file, f.summary AS summary
            """, rows=rows)
    }
    sem = asyncio.Semaphore(8)

    async def ask(row):
        d = details.get((row["sid"], row["fid"]))
        if not d:
            return False
        async with sem:
            try:
                resp = await llm.ainvoke(VERIFY_PROMPT.format(
                    heading=d["heading"], text=d["text"][:2000], qname=d["qname"],
                    file=d["file"], summary=d["summary"]))
                return (resp.content or "").strip().lower().startswith("yes")
            except Exception as err:
                log.warning("verify failed, keeping link: %s", err)
                return True

    verdicts = await asyncio.gather(*(ask(r) for r in rows))
    kept = [r for r, ok in zip(rows, verdicts) if ok]
    return kept, len(rows) - len(kept)
