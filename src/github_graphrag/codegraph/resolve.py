"""
Stage 4: resolve links between code nodes. No LLM.

    File     -[:IMPORTS]->      File      (Python import map)
    Class    -[:INHERITS]->     Class
    Function -[:CALLS {confidence, via}]->      Function
    Function -[:INSTANTIATES {confidence, via}]-> Class

A called expression like `security.verify_password` or `repo.find_by_email`
is resolved in order of confidence:

    1.0  self.x() / cls.x()          -> method of the same class (or a base class)
    1.0  imported name / module      -> the exact file the import points to
    1.0  same-file function or class
    0.9  typed variable              -> repo = UserRepository(db); repo.find_by_email()
    0.7  unique name in the repo     -> only one function has that name
    ---  anything ambiguous is skipped (no edge is better than a wrong edge)

On updates only the *affected* files are re-resolved: changed files, files
that import them, and files that call a name defined in them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from .store import GraphStore

log = logging.getLogger(__name__)

# Called names that are almost never your own code; skip name-only matching.
COMMON_NAMES = {
    "get", "set", "add", "update", "delete", "save", "load", "run", "execute",
    "commit", "query", "filter", "first", "all", "append", "extend", "items",
    "keys", "values", "format", "join", "split", "strip", "encode", "decode",
    "lower", "upper", "replace", "read", "write", "close", "open", "send",
    "print", "len", "str", "int", "dict", "list", "min", "max", "sum",
    "range", "isinstance", "super", "sorted", "hexdigest", "copy", "pop",
    "__init__", "main", "init", "setup", "start", "stop", "create", "process",
}


@dataclass
class _Fn:
    id: str
    name: str
    qname: str
    file: str
    class_id: str | None
    calls: list[str]
    var_types: dict[str, str]


class _Index:
    """Everything the resolver needs, loaded once per run."""

    def __init__(self, store: GraphStore, repo: str):
        files = store.run(
            "MATCH (f:File {repo: $repo}) RETURN f.id AS id, f.path AS path, "
            "f.module AS module, f.imports AS imports", repo=repo)
        classes = store.run(
            "MATCH (c:Class {repo: $repo}) RETURN c.id AS id, c.name AS name, "
            "c.qname AS qname, c.file AS file, c.bases AS bases", repo=repo)
        functions = store.run(
            "MATCH (f:Function {repo: $repo}) RETURN f.id AS id, f.name AS name, "
            "f.qname AS qname, f.file AS file, f.class_id AS class_id, "
            "f.calls AS calls, f.var_types AS var_types", repo=repo)

        self.file_id = {f["path"]: f["id"] for f in files}
        self.imports: dict[str, dict[str, tuple[str, str]]] = {}
        for f in files:
            table = {}
            for entry in f["imports"] or []:
                alias, _, target = entry.partition("=")
                module, _, symbol = target.partition(":")
                table[alias] = (module, symbol)
            self.imports[f["path"]] = table

        # module name -> file path (also registered under shorter suffixes,
        # because the import root may differ from the repo root)
        by_suffix: dict[str, set[str]] = defaultdict(set)
        for f in files:
            if not f["module"]:
                continue
            parts = f["module"].split(".")
            for i in range(len(parts)):
                by_suffix[".".join(parts[i:])].add(f["path"])
        self.module_file = {m: next(iter(p)) for m, p in by_suffix.items() if len(p) == 1}

        self.classes = {c["id"]: c for c in classes}
        self.top_level: dict[str, dict[str, str]] = defaultdict(dict)  # path -> name -> id
        for c in classes:
            if "." not in c["qname"]:
                self.top_level[c["file"]][c["name"]] = c["id"]

        self.functions = [
            _Fn(f["id"], f["name"], f["qname"], f["file"], f["class_id"],
                f["calls"] or [],
                dict(v.split("=", 1) for v in (f["var_types"] or []) if "=" in v))
            for f in functions
        ]
        self.methods: dict[str, dict[str, str]] = defaultdict(dict)   # class_id -> name -> fn id
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for fn in self.functions:
            if fn.class_id:
                self.methods[fn.class_id][fn.name] = fn.id
            elif "." not in fn.qname:
                self.top_level[fn.file][fn.name] = fn.id
            self.by_name[fn.name].append(fn.id)

        # self.<attr> types collected from every method of a class
        self.attr_types: dict[str, dict[str, str]] = defaultdict(dict)
        for fn in self.functions:
            if fn.class_id:
                for var, typ in fn.var_types.items():
                    if var.startswith("self."):
                        self.attr_types[fn.class_id][var[5:]] = typ

        self.bases: dict[str, list[str]] = {}
        for c in classes:
            self.bases[c["id"]] = [
                b for b in (self.resolve_symbol(c["file"], base) for base in c["bases"] or [])
                if b and b in self.classes
            ]

    # ------------------------------------------------------------ lookups

    def file_for_module(self, module: str) -> str | None:
        return self.module_file.get(module)

    def resolve_symbol(self, path: str, dotted: str) -> str | None:
        """Name as written in `path` ('UserRepository', 'security.verify_password',
        'models.User') -> node id of a Class or top-level Function, or None."""
        parts = dotted.split(".")
        head, rest = parts[0], parts[1:]

        if not rest and head in self.top_level.get(path, {}):
            return self.top_level[path][head]

        imported = self.imports.get(path, {}).get(head)
        if imported:
            module, symbol = imported
            if symbol:
                # from pkg.mod import name   OR   from pkg import submodule
                target_file = self.file_for_module(module)
                if target_file and not rest and symbol in self.top_level.get(target_file, {}):
                    return self.top_level[target_file][symbol]
                sub_file = self.file_for_module(f"{module}.{symbol}")
                if sub_file and len(rest) == 1:
                    return self.top_level.get(sub_file, {}).get(rest[0])
                if target_file and rest and symbol in self.top_level.get(target_file, {}):
                    # Class imported, attribute accessed: Class.method
                    cls = self.top_level[target_file][symbol]
                    return self.method(cls, rest[0]) if len(rest) == 1 else None
            else:
                # import pkg.mod as alias ; alias.func()
                target_file = self.file_for_module(".".join([module, *rest[:-1]]))
                if target_file and rest:
                    return self.top_level.get(target_file, {}).get(rest[-1])
        return None

    def method(self, class_id: str, name: str, _depth: int = 0) -> str | None:
        """Method lookup through the inheritance chain."""
        if class_id in self.methods and name in self.methods[class_id]:
            return self.methods[class_id][name]
        if _depth > 5:
            return None
        for base in self.bases.get(class_id, []):
            found = self.method(base, name, _depth + 1)
            if found:
                return found
        return None

    def class_of_type(self, path: str, type_name: str) -> str | None:
        target = self.resolve_symbol(path, type_name)
        return target if target in self.classes else None


def _resolve_call(ix: _Index, fn: _Fn, call: str) -> tuple[str, float, str] | None:
    """-> (target id, confidence, rule) or None."""
    if "()" in call:           # get_db().query(...) : return types unknown
        return None
    parts = call.split(".")
    head, last = parts[0], parts[-1]

    # 1. self.method() / cls.method()
    if head in ("self", "cls") and fn.class_id:
        if len(parts) == 2:
            target = ix.method(fn.class_id, last)
            if target:
                return target, 1.0, "self"
        elif len(parts) == 3:  # self.users.find_by_email()
            attr_type = ix.attr_types.get(fn.class_id, {}).get(parts[1])
            if attr_type:
                cls = ix.class_of_type(fn.file, attr_type)
                target = ix.method(cls, last) if cls else None
                if target:
                    return target, 0.9, "self_attr_type"

    # 2. typed local variable: repo = UserRepository(db); repo.find_by_email()
    if len(parts) == 2 and head in fn.var_types:
        cls = ix.class_of_type(fn.file, fn.var_types[head])
        target = ix.method(cls, last) if cls else None
        if target:
            return target, 0.9, "var_type"

    # 3. imported / same-file symbol
    target = ix.resolve_symbol(fn.file, call)
    if target:
        return target, 1.0, "import" if head in ix.imports.get(fn.file, {}) else "same_file"

    # 4. unique name in the repo
    if last not in COMMON_NAMES and not last.startswith("__"):
        candidates = [c for c in ix.by_name.get(last, []) if c != fn.id]
        if len(candidates) == 1:
            return candidates[0], 0.7, "unique_name"
    return None


# =========================================================================

def resolve_links(store: GraphStore, repo: str,
                  changed_file_ids: list[str] | None = None) -> dict:
    """Recompute IMPORTS / INHERITS / CALLS / INSTANTIATES.

    changed_file_ids=None  -> whole repo (first ingest)
    otherwise              -> only affected files
    """
    ix = _Index(store, repo)
    affected = _affected_paths(ix, store, repo, changed_file_ids)
    if not affected:
        return {"files": 0, "calls": 0, "instantiates": 0, "imports": 0, "inherits": 0}

    calls, inst, imports, inherits = [], [], [], []
    for fn in ix.functions:
        if fn.file not in affected:
            continue
        seen = set()
        for call in fn.calls:
            hit = _resolve_call(ix, fn, call)
            if not hit or hit[0] in seen or hit[0] == fn.id:
                continue
            seen.add(hit[0])
            row = {"src": fn.id, "dst": hit[0], "confidence": hit[1], "via": hit[2]}
            (inst if hit[0] in ix.classes else calls).append(row)

    for path in affected:
        for alias, (module, symbol) in ix.imports.get(path, {}).items():
            target = ix.file_for_module(module)
            sub = ix.file_for_module(f"{module}.{symbol}") if symbol else None
            for t in {target, sub} - {None, path}:
                imports.append({"src": ix.file_id[path], "dst": ix.file_id[t]})

    for cid, bases in ix.bases.items():
        if ix.classes[cid]["file"] in affected:
            inherits.extend({"src": cid, "dst": b} for b in bases)

    affected_ids = [ix.file_id[p] for p in affected if p in ix.file_id]
    store.run(
        """
        UNWIND $ids AS fid
        MATCH (f:File {id: fid})-[:DEFINES]->(n)-[r]->()
        WHERE type(r) = 'CALLS' OR type(r) = 'INSTANTIATES' OR type(r) = 'INHERITS'
        DELETE r
        """, ids=affected_ids)
    store.run(
        """
        UNWIND $ids AS fid
        MATCH (f:File {id: fid})-[r:IMPORTS]->()
        DELETE r
        """, ids=affected_ids)

    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (a:Function {id: row.src}), (b:Function {id: row.dst})
        MERGE (a)-[r:CALLS]->(b)
        SET r.confidence = row.confidence, r.via = row.via
        """, calls)
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (a:Function {id: row.src}), (b:Class {id: row.dst})
        MERGE (a)-[r:INSTANTIATES]->(b)
        SET r.confidence = row.confidence, r.via = row.via
        """, inst)
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (a:File {id: row.src}), (b:File {id: row.dst})
        MERGE (a)-[:IMPORTS]->(b)
        """, list({(r["src"], r["dst"]): r for r in imports}.values()))
    store.run_batched(
        """
        UNWIND $rows AS row
        MATCH (a:Class {id: row.src}), (b:Class {id: row.dst})
        MERGE (a)-[:INHERITS]->(b)
        """, inherits)

    return {"files": len(affected), "calls": len(calls), "instantiates": len(inst),
            "imports": len(imports), "inherits": len(inherits)}


def _affected_paths(ix: _Index, store: GraphStore, repo: str,
                    changed_file_ids: list[str] | None) -> set[str]:
    all_paths = set(ix.file_id)
    if changed_file_ids is None:
        return all_paths
    prefix = f"{repo}:"
    changed = {fid[len(prefix):] for fid in changed_file_ids if fid.startswith(prefix)}
    changed &= all_paths
    if not changed:
        return set()

    # names defined in changed files: callers of those names must be re-checked
    names = {fn.name for fn in ix.functions if fn.file in changed}
    names |= {c["name"] for c in ix.classes.values() if c["file"] in changed}
    changed_modules = {m for m, p in ix.module_file.items() if p in changed}

    affected = set(changed)
    for fn in ix.functions:
        if any(call.split(".")[-1] in names for call in fn.calls):
            affected.add(fn.file)
    for path, table in ix.imports.items():
        if any(mod in changed_modules or f"{mod}.{sym}" in changed_modules
               for mod, sym in table.values()):
            affected.add(path)
    return affected
