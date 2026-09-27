"""
Stage 3: write the parsed structure into Neo4j - for a new repo AND for updates.

The same function handles both flows:

    first run   : every file is "added"
    later runs  : compare each file's SHA with the one stored on its node
                  -> added / modified / deleted / unchanged

Only added and modified files are parsed and written. Inside a modified
file, functions are compared one by one via `content_hash`, so an unchanged
function keeps its summary, embedding and doc links.

A File's `sha` is written LAST, after all its children. If a run crashes
half-way, that file still looks "modified" next time and is simply redone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .discover import RepoFile
from .parse_code import ParsedCode, parse_code
from .parse_docs import ParsedDoc, parse_doc
from .store import GraphStore

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0
    changed_code_files: list[str] = field(default_factory=list)   # file ids
    functions_changed: int = 0
    sections_changed: int = 0
    docs_flagged_outdated: int = 0

    def summary(self) -> dict:
        return {
            "added": len(self.added), "modified": len(self.modified),
            "deleted": len(self.deleted), "unchanged": self.unchanged,
            "functions_changed": self.functions_changed,
            "sections_changed": self.sections_changed,
            "doc_links_flagged_outdated": self.docs_flagged_outdated,
        }


def sync_repo(store: GraphStore, repo: str, root: Path, files: list[RepoFile],
              commit: str | None = None) -> SyncResult:
    result = SyncResult()
    store.run(
        "MERGE (r:Repo {name: $repo}) SET r.root = $root, r.status = 'indexing'",
        repo=repo, root=str(root),
    )

    stored = {
        row["path"]: row["sha"]
        for label in ("File", "Doc")
        for row in store.run(
            f"MATCH (n:{label} {{repo: $repo}}) RETURN n.path AS path, n.sha AS sha",
            repo=repo,
        )
    }
    current = {f.path: f for f in files}

    to_parse: list[RepoFile] = []
    for path, f in current.items():
        if path not in stored:
            result.added.append(path)
            to_parse.append(f)
        elif stored[path] != f.sha:
            result.modified.append(path)
            to_parse.append(f)
        else:
            result.unchanged += 1
    result.deleted = [p for p in stored if p not in current]

    # ------------------------------------------------------------ deletions
    if result.deleted:
        _delete_paths(store, repo, result.deleted)

    # ------------------------------------------------------------ parse
    code: list[ParsedCode] = []
    docs: list[ParsedDoc] = []
    for f in to_parse:
        data = (root / f.path).read_bytes()
        if f.kind == "code":
            code.append(parse_code(repo, f.path, data.decode("utf-8", "replace"),
                                   f.language, f.sha, f.is_test))
        else:
            try:
                docs.append(parse_doc(repo, f.path, data, f.language, f.sha))
            except Exception as err:  # corrupt office file, missing optional lib
                log.warning("could not parse doc %s: %s", f.path, err)

    # ------------------------------------------------------------ write
    if code:
        _write_code(store, repo, code, result)
    if docs:
        _write_docs(store, repo, docs, result)

    _cleanup_orphans(store, repo)
    store.run("MATCH (r:Repo {name: $repo}) SET r.commit_pending = $commit",
              repo=repo, commit=commit)
    return result


def mark_repo_ready(store: GraphStore, repo: str, commit: str | None) -> None:
    """Move the commit pointer. Called only after every stage succeeded."""
    store.run(
        "MATCH (r:Repo {name: $repo}) "
        "SET r.indexed_commit = $commit, r.status = 'ready', r.commit_pending = null",
        repo=repo, commit=commit,
    )


# =========================================================================
# Code
# =========================================================================

WRITE_FILES = """
UNWIND $rows AS row
MATCH (r:Repo {name: row.repo})
MERGE (f:File {id: row.id})
SET f.repo = row.repo, f.path = row.path, f.language = row.language,
    f.is_test = row.is_test, f.module = row.module, f.imports = row.imports,
    f.header = row.header
MERGE (r)-[:CONTAINS]->(f)
"""

WRITE_CLASSES = """
UNWIND $rows AS row
MATCH (f:File {id: row.file_id})
MERGE (c:Class {id: row.id})
SET c += row
MERGE (f)-[:DEFINES]->(c)
"""

# Upsert. Summary / embedding are not in `row`, so they survive; enrich
# notices changed code because summary_hash no longer equals content_hash.
WRITE_FUNCTIONS = """
UNWIND $rows AS row
MATCH (f:File {id: row.file_id})
MERGE (fn:Function {id: row.id})
WITH f, fn, row,
     (fn.content_hash IS NULL OR fn.content_hash <> row.content_hash) AS changed
SET fn += row
MERGE (f)-[:DEFINES]->(fn)
RETURN count(CASE WHEN changed THEN 1 END) AS changed
"""

FLAG_OUTDATED_DOC_LINKS = """
UNWIND $rows AS row
MATCH (s:Section)-[l]->(fn:Function {id: row.id})
WHERE (type(l) = 'MENTIONS' OR type(l) = 'RELATES_TO')
  AND fn.content_hash <> row.content_hash
SET l.doc_may_be_outdated = true
RETURN count(l) AS flagged
"""

LINK_METHODS = """
UNWIND $rows AS row
MATCH (fn:Function {id: row.id})
OPTIONAL MATCH (:Class)-[old:HAS_METHOD]->(fn)
DELETE old
WITH fn, row WHERE row.class_id IS NOT NULL
MATCH (c:Class {id: row.class_id})
MERGE (c)-[:HAS_METHOD]->(fn)
"""

PRUNE_FILE_CHILDREN = """
UNWIND $rows AS row
MATCH (f:File {id: row.file_id})-[:DEFINES]->(n)
WHERE NOT n.id IN row.keep
DETACH DELETE n
"""

CLEAR_HANDLES = """
UNWIND $file_ids AS fid
MATCH (:File {id: fid})-[:DEFINES]->(:Function)-[h:HANDLES]->(:Endpoint)
DELETE h
"""

WRITE_ENDPOINTS = """
UNWIND $rows AS row
MATCH (fn:Function {id: row.function_id})
MERGE (e:Endpoint {id: row.id})
SET e.repo = row.repo, e.method = row.method, e.route = row.route
MERGE (fn)-[:HANDLES]->(e)
"""

SET_FILE_SHA = """
UNWIND $rows AS row
MATCH (f:File {id: row.id}) SET f.sha = row.sha
"""


def _write_code(store: GraphStore, repo: str, parsed: list[ParsedCode],
                result: SyncResult) -> None:
    files = [p.file for p in parsed]
    classes = [c for p in parsed for c in p.classes]
    functions = [fn for p in parsed for fn in p.functions]
    endpoints = [e for p in parsed for e in p.endpoints]

    store.run_batched(WRITE_FILES, files)

    # remove classes/functions that no longer exist in these files
    keep = [
        {"file_id": p.file["id"],
         "keep": [c["id"] for c in p.classes] + [f["id"] for f in p.functions]}
        for p in parsed
    ]
    store.run_batched(PRUNE_FILE_CHILDREN, keep)

    store.run_batched(WRITE_CLASSES, classes)

    # flag doc links BEFORE overwriting content_hash
    for i in range(0, len(functions), 500):
        batch = functions[i : i + 500]
        flagged = store.run(FLAG_OUTDATED_DOC_LINKS, rows=batch)
        result.docs_flagged_outdated += flagged[0]["flagged"] if flagged else 0
        changed = store.run(WRITE_FUNCTIONS, rows=batch)
        result.functions_changed += changed[0]["changed"] if changed else 0

    store.run_batched(LINK_METHODS, [{"id": f["id"], "class_id": f["class_id"]}
                                     for f in functions])
    store.run(CLEAR_HANDLES, file_ids=[f["id"] for f in files])
    store.run_batched(WRITE_ENDPOINTS, endpoints)
    store.run_batched(SET_FILE_SHA, [{"id": f["id"], "sha": f["sha"]} for f in files])

    result.changed_code_files = [f["id"] for f in files]


# =========================================================================
# Docs
# =========================================================================

WRITE_DOCS = """
UNWIND $rows AS row
MATCH (r:Repo {name: row.repo})
MERGE (d:Doc {id: row.id})
SET d.repo = row.repo, d.path = row.path, d.kind = row.kind,
    d.doc_type = row.doc_type, d.title = row.title
MERGE (r)-[:CONTAINS]->(d)
"""

PRUNE_SECTIONS = """
UNWIND $rows AS row
MATCH (d:Doc {id: row.doc_id})-[:HAS_SECTION]->(s:Section)
WHERE NOT s.id IN row.keep
DETACH DELETE s
"""

WRITE_SECTIONS = """
UNWIND $rows AS row
MATCH (d:Doc {id: row.doc_id})
MERGE (s:Section {id: row.id})
WITH d, s, row,
     (s.content_hash IS NULL OR s.content_hash <> row.content_hash) AS changed
SET s += row
SET s.needs_embedding = CASE WHEN changed THEN true ELSE coalesce(s.needs_embedding, false) END
MERGE (d)-[:HAS_SECTION]->(s)
RETURN count(CASE WHEN changed THEN 1 END) AS changed
"""

CLEAR_CHANGED_SECTION_LINKS = """
UNWIND $ids AS sid
MATCH (s:Section {id: sid})-[l]->()
WHERE s.needs_embedding = true AND (type(l) = 'MENTIONS' OR type(l) = 'RELATES_TO')
DELETE l
"""

SET_DOC_SHA = """
UNWIND $rows AS row
MATCH (d:Doc {id: row.id}) SET d.sha = row.sha
"""


def _write_docs(store: GraphStore, repo: str, parsed: list[ParsedDoc],
                result: SyncResult) -> None:
    store.run_batched(WRITE_DOCS, [p.doc for p in parsed])
    store.run_batched(PRUNE_SECTIONS, [
        {"doc_id": p.doc["id"], "keep": [s["id"] for s in p.sections]} for p in parsed
    ])
    sections = [s for p in parsed for s in p.sections]
    for i in range(0, len(sections), 500):
        changed = store.run(WRITE_SECTIONS, rows=sections[i : i + 500])
        result.sections_changed += changed[0]["changed"] if changed else 0
    # links of edited sections are recomputed by the bridge stage
    store.run(CLEAR_CHANGED_SECTION_LINKS, ids=[s["id"] for s in sections])
    store.run_batched(SET_DOC_SHA, [{"id": p.doc["id"], "sha": p.doc["sha"]} for p in parsed])


# =========================================================================
# Deletes & cleanup
# =========================================================================

def _delete_paths(store: GraphStore, repo: str, paths: list[str]) -> None:
    ids = [f"{repo}:{p}" for p in paths]
    store.run(
        """
        UNWIND $ids AS id
        MATCH (f:File {id: id})
        OPTIONAL MATCH (f)-[:DEFINES]->(n)
        DETACH DELETE n
        """,
        ids=ids,
    )
    store.run(
        """
        UNWIND $ids AS id
        MATCH (d:Doc {id: id})
        OPTIONAL MATCH (d)-[:HAS_SECTION]->(s)
        DETACH DELETE s
        """,
        ids=ids,
    )
    for label in ("File", "Doc"):
        store.run(f"UNWIND $ids AS id MATCH (n:{label} {{id: id}}) DETACH DELETE n", ids=ids)


def _cleanup_orphans(store: GraphStore, repo: str) -> None:
    # endpoints no function handles any more
    store.run(
        """
        MATCH (e:Endpoint {repo: $repo})
        WHERE NOT ()-[:HANDLES]->(e)
        DETACH DELETE e
        """,
        repo=repo,
    )


def delete_repo(store: GraphStore, repo: str) -> int:
    """Remove every node of a repo (used by `codegraph delete-repo`)."""
    total = 0
    for label in ("Section", "Doc", "Function", "Class", "File", "Endpoint", "Module"):
        rows = store.run(
            f"MATCH (n:{label} {{repo: $repo}}) DETACH DELETE n RETURN count(*) AS total",
            repo=repo,
        )
        total += rows[0]["total"] if rows else 0
    store.run("MATCH (r:Repo {name: $repo}) DETACH DELETE r", repo=repo)
    return total
