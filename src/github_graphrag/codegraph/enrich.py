"""
Stage 5 - Enrich: a 2-sentence plain-English summary for every Function.

Only functions whose code changed are summarised (summary_hash <> content_hash),
and summaries are cached in :SummaryCache nodes keyed by
(code hash + model + prompt version). Renames, reverts and re-ingests of
identical code never call the LLM again. Identical code in the same run
shares one call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from .config import model_id
from .store import GraphStore

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"     # bump when SUMMARY_PROMPT changes
MIN_LINES = 3             # shorter functions: docstring/code used as summary, no LLM
MAX_CODE_CHARS = 6000
MAX_ATTEMPTS = 4

SUMMARY_PROMPT = """You are documenting a codebase for a search index.

Repository: {repo}
File: {file}
Symbol: {qname}
Signature: {signature}
Docstring: {docstring}
Calls: {calls}

Code:
```
{code}
```

In exactly 2 plain-English sentences, say what this code does and why it
exists, as you would explain it to a product manager. Mention the business
concept (for example "login", "claim approval", "invoice upload") when it is
clear from the code. Do not repeat the signature. Do not start with
"This function". Return only the 2 sentences."""


@dataclass
class _Pending:
    id: str
    repo: str
    file: str
    qname: str
    signature: str
    docstring: str
    code: str
    content_hash: str
    calls: list[str]


SELECT_PENDING = """
MATCH (f:Function {repo: $repo})
WHERE (f.summary IS NULL OR f.summary_hash <> f.content_hash)
  AND f.end_line - f.start_line + 1 >= $min_lines
OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
RETURN f.id AS id, f.repo AS repo, f.file AS file, f.qname AS qname,
       coalesce(f.signature, '') AS signature,
       coalesce(f.docstring, '') AS docstring,
       f.code AS code, f.content_hash AS content_hash,
       collect(DISTINCT callee.qname) AS calls
"""

LOAD_CACHE = """
UNWIND $keys AS k
MATCH (c:SummaryCache {key: k})
RETURN c.key AS key, c.summary AS summary
"""

WRITE_SUMMARIES = """
UNWIND $rows AS row
MATCH (f:Function {id: row.id})
WHERE f.content_hash = row.content_hash
SET f.summary = row.summary,
    f.summary_hash = row.content_hash,
    f.needs_embedding = true
WITH row
MERGE (c:SummaryCache {key: row.cache_key})
SET c.summary = row.summary
"""


def cache_key(content_hash: str, model: str) -> str:
    return hashlib.sha256(f"{content_hash}|{model}|{PROMPT_VERSION}".encode()).hexdigest()


def build_prompt(fn: _Pending) -> str:
    code = fn.code or ""
    if len(code) > MAX_CODE_CHARS:
        code = code[:MAX_CODE_CHARS] + "\n# ... (truncated)"
    return SUMMARY_PROMPT.format(
        repo=fn.repo, file=fn.file, qname=fn.qname,
        signature=fn.signature or "(unknown)",
        docstring=fn.docstring or "(none)",
        calls=", ".join(fn.calls[:15]) or "(none)",
        code=code,
    )


async def _summarize(llm, fn: _Pending, sem: asyncio.Semaphore) -> str | None:
    prompt = build_prompt(fn)
    async with sem:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await llm.ainvoke(prompt)
                text = " ".join((response.content or "").split()).strip('"').strip()
                if text:
                    return text
                raise ValueError("empty summary")
            except Exception as err:  # rate limit, 503, bad output
                if attempt == MAX_ATTEMPTS:
                    log.warning("summary failed for %s: %s", fn.id, err)
                    return None
                await asyncio.sleep(2 ** attempt)
    return None


# Short functions: no LLM call; their docstring or code is summary enough.
SUMMARISE_SHORT = """
MATCH (f:Function {repo: $repo})
WHERE (f.summary IS NULL OR f.summary_hash <> f.content_hash)
  AND f.end_line - f.start_line + 1 < $min_lines
SET f.summary = CASE WHEN coalesce(f.docstring, '') <> '' THEN f.docstring
                     ELSE left(f.code, 300) END,
    f.summary_hash = f.content_hash,
    f.needs_embedding = true
RETURN count(f) AS n
"""


async def enrich_functions(store: GraphStore, llm, repo: str, concurrency: int = 8) -> dict:
    short = store.run(SUMMARISE_SHORT, repo=repo, min_lines=MIN_LINES)
    short_count = short[0]["n"] if short else 0
    model = model_id(llm)
    pending = [_Pending(**r) for r in store.run(SELECT_PENDING, repo=repo, min_lines=MIN_LINES)]
    if not pending:
        return {"pending": 0, "from_cache": 0, "llm_calls": 0, "failed": 0,
                "short_no_llm": short_count}

    keys = {fn.id: cache_key(fn.content_hash, model) for fn in pending}
    cached = {r["key"]: r["summary"]
              for r in store.run(LOAD_CACHE, keys=list(set(keys.values())))}

    rows: list[dict] = []
    waiting: dict[str, list[_Pending]] = {}
    for fn in pending:
        key = keys[fn.id]
        if key in cached:
            rows.append(_row(fn, key, cached[key]))
        else:
            waiting.setdefault(key, []).append(fn)

    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*(_summarize(llm, g[0], sem) for g in waiting.values()))

    failed = 0
    for (key, group), summary in zip(waiting.items(), results):
        if summary is None:
            failed += len(group)
            continue
        rows.extend(_row(fn, key, summary) for fn in group)

    store.run_batched(WRITE_SUMMARIES, rows, size=100)
    return {
        "pending": len(pending),
        "from_cache": len(pending) - sum(len(g) for g in waiting.values()),
        "llm_calls": len(waiting),
        "failed": failed,
        "short_no_llm": short_count,
    }


def _row(fn: _Pending, key: str, summary: str) -> dict:
    return {"id": fn.id, "content_hash": fn.content_hash, "cache_key": key, "summary": summary}
