"""
Question answering over the code graph.

A question is routed to one of three strategies:

    impact    "what breaks if I change X" / "who calls X"
              -> CALLS*1..3 in Cypher, no vector search
    endpoint  "who handles POST /claims"
              -> Endpoint <-HANDLES- Function lookup
    explain   everything else
              -> vector search (functions, doc sections, modules)
              -> expand along the graph (callers, callees, linked docs)
              -> context with code and file:line -> LLM answer
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .embed import embed_query
from .store import GraphStore

MAX_CODE_IN_CONTEXT = 1500
IMPACT_PATTERN = re.compile(
    r"(what (breaks|happens|is affected)|impact|blast radius|who calls|callers? of|"
    r"used by|depends on|if i (change|modify|delete|remove))", re.I)
ENDPOINT_PATTERN = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", re.I)
IDENT_PATTERN = re.compile(r"`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`?")

ANSWER_PROMPT = """You answer questions about a software repository using ONLY the context.

Rules:
- Cite code as `file:start-end` and docs as their heading.
- If a doc link is marked "may be outdated", say so.
- If the context is not enough, say what is missing. Do not invent code.

QUESTION:
{question}

CONTEXT:
{context}
"""


@dataclass
class Answer:
    route: str
    context: str
    answer: str | None = None
    sources: list[str] = field(default_factory=list)


# =========================================================================
# Impact
# =========================================================================

def find_functions(store: GraphStore, repo: str, name: str) -> list[dict]:
    return store.run(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.qname = $name OR f.name = $name OR f.id = $name
        RETURN f.id AS id, f.qname AS qname, f.file AS file,
               f.start_line AS start, f.end_line AS end
        ORDER BY f.qname
        """, repo=repo, name=name)


def impact(store: GraphStore, repo: str, name: str, depth: int = 3,
           min_confidence: float = 0.7) -> dict:
    targets = find_functions(store, repo, name)
    if not targets:
        return {"targets": [], "callers": [], "endpoints": [], "tests": [], "docs": []}
    ids = [t["id"] for t in targets]
    depth = max(1, min(int(depth), 5))

    callers = store.run(
        f"""
        MATCH p = (c:Function)-[:CALLS*1..{depth}]->(t:Function)
        WHERE t.id IN $ids AND all(r IN relationships(p) WHERE r.confidence >= $minc)
        WITH c, min(length(p)) AS hops
        RETURN c.id AS id, c.qname AS qname, c.file AS file, c.start_line AS start,
               c.end_line AS end, c.is_test AS is_test, hops
        ORDER BY hops, c.file, c.qname
        """, ids=ids, minc=min_confidence)
    affected = ids + [c["id"] for c in callers]

    endpoints = store.run(
        """
        MATCH (f:Function)-[:HANDLES]->(e:Endpoint)
        WHERE f.id IN $ids
        RETURN DISTINCT e.method + ' ' + e.route AS endpoint, f.qname AS handler
        """, ids=affected)
    docs = store.run(
        """
        MATCH (s:Section)-[r]->(f:Function)
        WHERE f.id IN $ids AND (type(r) = 'MENTIONS' OR type(r) = 'RELATES_TO')
        RETURN DISTINCT s.path AS doc, s.heading AS heading, f.qname AS about,
               coalesce(r.doc_may_be_outdated, false) AS outdated
        """, ids=affected)
    return {
        "targets": targets,
        "callers": [c for c in callers if not c["is_test"]],
        "tests": [c for c in callers if c["is_test"]],
        "endpoints": endpoints,
        "docs": docs,
    }


def format_impact(name: str, result: dict) -> str:
    if not result["targets"]:
        return f"No function named `{name}` found."
    lines = ["Changing:"]
    lines += [f"  - {t['qname']}  ({t['file']}:{t['start']}-{t['end']})" for t in result["targets"]]
    lines.append(f"\nCallers ({len(result['callers'])}):")
    lines += [f"  - [{c['hops']} hop] {c['qname']}  ({c['file']}:{c['start']}-{c['end']})"
              for c in result["callers"]] or ["  (none)"]
    lines.append(f"\nAPI endpoints affected ({len(result['endpoints'])}):")
    lines += [f"  - {e['endpoint']}  (via {e['handler']})" for e in result["endpoints"]] or ["  (none)"]
    lines.append(f"\nTests to run ({len(result['tests'])}):")
    lines += [f"  - {c['qname']}  ({c['file']})" for c in result["tests"]] or ["  (none found)"]
    lines.append(f"\nDocs to review ({len(result['docs'])}):")
    lines += [f"  - {d['doc']} > {d['heading']}  (about {d['about']})"
              + ("  [may be outdated]" if d["outdated"] else "")
              for d in result["docs"]] or ["  (none linked)"]
    return "\n".join(lines)


# =========================================================================
# Explain (vector + graph)
# =========================================================================

def build_context(store: GraphStore, embedder, repo: str, question: str,
                  k_functions: int = 6, k_sections: int = 4, k_modules: int = 2) -> tuple[str, list[str]]:
    vector = embed_query(embedder, question)
    fn_hits = store.vector_search("Function", vector, k_functions, repo)
    sec_hits = store.vector_search("Section", vector, k_sections, repo)
    mod_hits = store.vector_search("Module", vector, k_modules, repo)

    # docs pull in the code they are linked to
    linked = store.run(
        """
        MATCH (s:Section)-[r]->(f:Function)
        WHERE s.id IN $ids AND (type(r) = 'MENTIONS' OR type(r) = 'RELATES_TO')
        RETURN DISTINCT f.id AS id
        """, ids=[h["id"] for h in sec_hits])
    fn_ids = list(dict.fromkeys([h["id"] for h in fn_hits] + [r["id"] for r in linked]))[:10]

    functions = store.run(
        """
        MATCH (f:Function) WHERE f.id IN $ids
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
        OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
        OPTIONAL MATCH (f)-[:HANDLES]->(e:Endpoint)
        RETURN f.id AS id, f.qname AS qname, f.file AS file, f.start_line AS start,
               f.end_line AS end, f.signature AS signature, f.summary AS summary,
               f.code AS code,
               collect(DISTINCT caller.qname) AS callers,
               collect(DISTINCT callee.qname) AS callees,
               collect(DISTINCT e.method + ' ' + e.route) AS endpoints
        """, ids=fn_ids)
    order = {fid: i for i, fid in enumerate(fn_ids)}
    functions.sort(key=lambda f: order.get(f["id"], 99))

    sections = store.run(
        """
        MATCH (s:Section) WHERE s.id IN $ids
        OPTIONAL MATCH (s)-[r]->(f:Function)
        RETURN s.id AS id, s.path AS path, s.heading AS heading, s.text AS text,
               collect(DISTINCT f.qname) AS about,
               any(x IN collect(coalesce(r.doc_may_be_outdated, false)) WHERE x) AS outdated
        """, ids=[h["id"] for h in sec_hits])
    modules = store.run(
        """
        MATCH (m:Module) WHERE m.id IN $ids
        OPTIONAL MATCH (m)-[:DEPENDS_ON]->(d:Module)
        RETURN m.name AS name, m.summary AS summary, collect(d.name) AS depends_on
        """, ids=[h["id"] for h in mod_hits])

    parts, sources = [], []
    if modules:
        parts.append("## Modules")
        for m in modules:
            deps = f" (depends on: {', '.join(m['depends_on'])})" if m["depends_on"] else ""
            parts.append(f"- {m['name']}: {m['summary']}{deps}")
    if sections:
        parts.append("\n## Documentation")
        for s in sections:
            flag = "  [linked code changed since - may be outdated]" if s["outdated"] else ""
            about = f"\n(about: {', '.join(s['about'])})" if s["about"] else ""
            parts.append(f"### {s['path']} > {s['heading']}{flag}\n{s['text'][:1200]}{about}")
            sources.append(f"{s['path']} > {s['heading']}")
    if functions:
        parts.append("\n## Code")
        for f in functions:
            code = (f["code"] or "")[:MAX_CODE_IN_CONTEXT]
            meta = []
            if f["endpoints"]:
                meta.append(f"handles: {', '.join(f['endpoints'])}")
            if f["callers"]:
                meta.append(f"called by: {', '.join(f['callers'][:6])}")
            if f["callees"]:
                meta.append(f"calls: {', '.join(f['callees'][:6])}")
            parts.append(
                f"### {f['qname']}  ({f['file']}:{f['start']}-{f['end']})\n"
                f"summary: {f['summary'] or '(none)'}\n" + "\n".join(meta) +
                f"\n```\n{code}\n```")
            sources.append(f"{f['file']}:{f['start']}-{f['end']}")
    return "\n".join(parts), sources


async def ask(store: GraphStore, llm, embedder, repo: str, question: str) -> Answer:
    # 1. impact questions
    if IMPACT_PATTERN.search(question):
        for candidate in _identifiers(question):
            result = impact(store, repo, candidate)
            if result["targets"]:
                return Answer("impact", format_impact(candidate, result))

    # 2. endpoint questions
    m = ENDPOINT_PATTERN.search(question)
    if m:
        rows = store.run(
            """
            MATCH (f:Function)-[:HANDLES]->(e:Endpoint {repo: $repo})
            WHERE e.method = $method AND e.route = $route
            RETURN f.qname AS qname, f.file AS file, f.start_line AS start, f.end_line AS end
            """, repo=repo, method=m.group(1).upper(), route=m.group(2).rstrip("?.,"))
        if rows:
            text = "\n".join(f"- {r['qname']}  ({r['file']}:{r['start']}-{r['end']})" for r in rows)
            context, sources = build_context(store, embedder, repo, question)
            answer = await _llm_answer(llm, question, f"## Handler\n{text}\n\n{context}")
            return Answer("endpoint", context, answer, sources)

    # 3. explain
    context, sources = build_context(store, embedder, repo, question)
    return Answer("explain", context, await _llm_answer(llm, question, context), sources)


async def _llm_answer(llm, question: str, context: str) -> str:
    response = await llm.ainvoke(ANSWER_PROMPT.format(question=question, context=context))
    return response.content


def _identifiers(question: str) -> list[str]:
    """Likely code names in a question, most specific first."""
    ticked = re.findall(r"`([^`]+)`", question)
    words = [w for w in IDENT_PATTERN.findall(question)
             if "_" in w or "." in w or re.search(r"[a-z][A-Z]", w)]
    return list(dict.fromkeys(ticked + words + question.replace("?", " ").split()))
