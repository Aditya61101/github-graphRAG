"""
Stage 2 (code): turn one source file into rows for File / Class / Function /
Endpoint nodes. No LLM involved.

- Python: parsed with the standard-library `ast` module. Exact, and gives
  imports, decorators (API routes), base classes and calls *with their
  receiver* (`sec.verify_password`, `self.issue_jwt`), which makes call
  resolution in stage 4 precise.
- Other languages (JS/TS/Java/Go): parsed with tree-sitter via
  `treesitter-chunker`. Functions, classes and called names; no import map,
  so their calls are resolved by name only.

IDs are stable and human-readable:
    File      <repo>:<path>
    Class     <repo>:<path>:<Class>
    Function  <repo>:<path>:<Class.method>   or  <repo>:<path>:<func>
    Endpoint  <repo>:<METHOD> <route>
"""

from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_CODE_CHARS = 20_000      # stored on the node; enrich truncates further
MAX_HEADER_CHARS = 3_000
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class ParsedCode:
    file: dict
    classes: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    endpoints: list[dict] = field(default_factory=list)


def parse_code(repo: str, path: str, text: str, language: str, sha: str,
               is_test: bool = False) -> ParsedCode:
    file_row = {
        "id": f"{repo}:{path}",
        "repo": repo,
        "path": path,
        "language": language,
        "sha": sha,
        "is_test": is_test,
        "module": module_name(path) if language == "python" else None,
        "imports": [],
        "header": "",
    }
    if language == "python":
        try:
            return _parse_python(repo, path, text, file_row)
        except SyntaxError as err:
            log.warning("syntax error in %s (%s); falling back to tree-sitter", path, err)
    return _parse_treesitter(repo, path, text, language, file_row)


def module_name(path: str) -> str:
    """backend/app/core/security.py -> backend.app.core.security"""
    mod = path[:-3] if path.endswith(".py") else path
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    return mod.replace("/", ".")


# =========================================================================
# Python (ast)
# =========================================================================

def _parse_python(repo: str, path: str, text: str, file_row: dict) -> ParsedCode:
    tree = ast.parse(text)
    lines = text.splitlines()
    out = ParsedCode(file=file_row)

    file_row["imports"] = _python_imports(tree, file_row["module"], path)
    file_row["header"] = _python_header(tree, lines)

    def src(node) -> str:
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        return "\n".join(lines[start - 1 : node.end_lineno])

    def visit(body, prefix: list[str], class_id: str | None):
        for node in body:
            if isinstance(node, ast.ClassDef):
                qname = ".".join(prefix + [node.name])
                cid = f"{repo}:{path}:{qname}"
                header = _class_header(node, lines)
                out.classes.append({
                    "id": cid, "repo": repo, "file": path, "file_id": file_row["id"],
                    "name": node.name, "qname": qname,
                    "bases": [_dotted(b) for b in node.bases if _dotted(b)],
                    "docstring": ast.get_docstring(node) or "",
                    "header": header,
                    "start_line": node.lineno, "end_line": node.end_lineno,
                    "content_hash": sha256(header),
                })
                visit(node.body, prefix + [node.name], cid)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = ".".join(prefix + [node.name])
                fid = f"{repo}:{path}:{qname}"
                code = src(node)
                decorators = [ast.unparse(d) for d in node.decorator_list]
                out.functions.append({
                    "id": fid, "repo": repo, "file": path, "file_id": file_row["id"],
                    "name": node.name, "qname": qname,
                    "class_id": class_id,
                    "signature": _signature(node),
                    "docstring": ast.get_docstring(node) or "",
                    "decorators": decorators,
                    "calls": _calls(node),
                    "var_types": _var_types(node),
                    "code": code[:MAX_CODE_CHARS],
                    "start_line": node.lineno, "end_line": node.end_lineno,
                    "content_hash": sha256(code),
                    "is_test": file_row["is_test"],
                })
                for method, route in _routes(node):
                    out.endpoints.append({
                        "id": f"{repo}:{method} {route}", "repo": repo,
                        "method": method, "route": route, "function_id": fid,
                    })
                # nested functions become their own nodes (no class)
                visit(node.body, prefix + [node.name], None)

    visit(tree.body, [], None)
    return out


def _python_imports(tree: ast.Module, module: str, path: str) -> list[str]:
    """Import map stored on the File node as 'alias=module:symbol' strings.

    `from app.core import security as sec` -> 'sec=app.core:security'
    `from .models import User`            -> 'User=<pkg>.models:User'
    `import os.path as p`                 -> 'p=os.path:'
    """
    package = module.split(".")[:-1] if not path.endswith("__init__.py") else module.split(".")
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                alias = a.asname or a.name.split(".")[0]
                target = a.name if a.asname else a.name.split(".")[0]
                result.append(f"{alias}={target}:")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import
                parent = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join([*parent, base] if base else parent)
            for a in node.names:
                if a.name == "*":
                    continue
                result.append(f"{a.asname or a.name}={base}:{a.name}")
    return sorted(set(result))


def _python_header(tree: ast.Module, lines: list[str]) -> str:
    """Imports + top-level statements (app = FastAPI(), include_router(...), constants)."""
    parts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        parts.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    return "\n".join(parts)[:MAX_HEADER_CHARS]


def _class_header(node: ast.ClassDef, lines: list[str]) -> str:
    """Class line + docstring + method signatures (not the full body)."""
    head = [lines[node.lineno - 1].strip()]
    doc = ast.get_docstring(node)
    if doc:
        head.append(f'    """{doc}"""')
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            head.append(f"    def {item.name}{_signature(item)}")
    return "\n".join(head)


def _signature(node) -> str:
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}({args}){ret}"


def _dotted(node) -> str | None:
    """Name / Attribute chain -> 'a.b.c'; anything else -> None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Call):          # get_db().query -> 'get_db().query'
        inner = _dotted(node.func)
        if inner:
            parts.append(inner + "()")
            return ".".join(reversed(parts))
    return None


def _calls(func_node) -> list[str]:
    """Called expressions inside this function, excluding nested defs."""
    calls: list[str] = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                name = _dotted(child.func)
                if name:
                    calls.append(name)
            walk(child)

    for stmt in func_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        walk(stmt)
    # keep order, drop duplicates
    return list(dict.fromkeys(calls))


def _var_types(func_node) -> list[str]:
    """Cheap local type hints, stored as 'name=Type' strings.

    def __init__(self, users: UserRepository): self.users = users
        -> 'users=UserRepository', 'self.users=UserRepository'
    repo = UserRepository(db)
        -> 'repo=UserRepository'
    """
    types: dict[str, str] = {}
    args = func_node.args
    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if a.annotation is not None:
            ann = _dotted(a.annotation)
            if ann:
                types[a.arg] = ann

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                value = child.value
                vtype = None
                if isinstance(child, ast.AnnAssign):
                    vtype = _dotted(child.annotation)
                if vtype is None and isinstance(value, ast.Call):
                    callee = _dotted(value.func)
                    # only constructor-looking calls: Capitalized last part
                    if callee and callee.rsplit(".", 1)[-1][:1].isupper():
                        vtype = callee
                if vtype is None and isinstance(value, ast.Name) and value.id in types:
                    vtype = types[value.id]
                if vtype:
                    for t in targets:
                        name = _dotted(t)
                        if name and name.count(".") <= 1:
                            types[name] = vtype
            visit(child)

    for stmt in func_node.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visit(ast.Module(body=[stmt], type_ignores=[]))
    return [f"{k}={v}" for k, v in types.items()]


def _routes(func_node) -> list[tuple[str, str]]:
    """FastAPI / Flask style decorators -> [(METHOD, '/path')]."""
    routes = []
    for dec in func_node.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        verb = dec.func.attr.lower()
        path_arg = dec.args[0] if dec.args else None
        if not (isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str)):
            continue
        route = path_arg.value
        if verb in HTTP_METHODS:
            routes.append((verb.upper(), route))
        elif verb == "route":  # Flask: @app.route("/x", methods=["POST"])
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value.upper() for e in kw.value.elts
                               if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            routes.extend((m, route) for m in methods)
    return routes


# =========================================================================
# Other languages (tree-sitter via treesitter-chunker)
# =========================================================================

FUNCTION_TYPES = {
    "function_definition", "function_declaration", "method_definition",
    "method_declaration", "arrow_function", "function_expression",
    "generator_function_declaration",
}
CLASS_TYPES = {
    "class_definition", "class_declaration", "interface_declaration",
    "abstract_class_declaration", "type_declaration",
}


def _parse_treesitter(repo: str, path: str, text: str, language: str,
                      file_row: dict) -> ParsedCode:
    from chunker import chunk_text

    out = ParsedCode(file=file_row)
    try:
        chunks = chunk_text(text, language, file_path=path)
    except Exception as err:  # unsupported grammar, parser crash
        log.warning("tree-sitter could not parse %s (%s); file node only", path, err)
        return out

    for c in chunks:
        names = _named_route(c.qualified_route)
        if not names:
            continue
        kind = c.node_type
        qname = ".".join(n for _, n in names)
        if kind in CLASS_TYPES:
            out.classes.append({
                "id": f"{repo}:{path}:{qname}", "repo": repo, "file": path,
                "file_id": file_row["id"], "name": names[-1][1], "qname": qname,
                "bases": [], "docstring": "",
                "header": c.content.splitlines()[0][:500] if c.content else "",
                "start_line": c.start_line, "end_line": c.end_line,
                "content_hash": sha256(c.content.splitlines()[0] if c.content else ""),
            })
        elif kind in FUNCTION_TYPES:
            parent_class = next(
                (".".join(n for _, n in names[: i + 1])
                 for i in range(len(names) - 2, -1, -1) if names[i][0] in CLASS_TYPES),
                None,
            )
            sig = (c.metadata or {}).get("signature") or {}
            out.functions.append({
                "id": f"{repo}:{path}:{qname}", "repo": repo, "file": path,
                "file_id": file_row["id"], "name": names[-1][1], "qname": qname,
                "class_id": f"{repo}:{path}:{parent_class}" if parent_class else None,
                "signature": _ts_signature(sig),
                "docstring": (c.metadata or {}).get("docstring") or "",
                "decorators": [],
                "calls": list(dict.fromkeys((c.metadata or {}).get("calls") or [])),
                "var_types": [],
                "code": c.content[:MAX_CODE_CHARS],
                "start_line": c.start_line, "end_line": c.end_line,
                "content_hash": sha256(c.content),
                "is_test": file_row["is_test"],
            })
    # de-duplicate ids (e.g. overloaded methods) keeping the first
    out.functions = list({f["id"]: f for f in reversed(out.functions)}.values())[::-1]
    out.classes = list({c["id"]: c for c in reversed(out.classes)}.values())[::-1]
    return out


def _named_route(route: list[str]) -> list[tuple[str, str]]:
    """['export_statement:anon@3', 'class_declaration:Auth', 'method_definition:verify']
       -> [('class_declaration','Auth'), ('method_definition','verify')]
       Arrow functions take the name of their variable (const handler = () => ...)."""
    named: list[tuple[str, str]] = []
    pending_var = None
    for part in route or []:
        kind, _, name = part.partition(":")
        if kind == "variable_declarator" and not name.startswith("anon"):
            pending_var = name
            continue
        if kind not in FUNCTION_TYPES and kind not in CLASS_TYPES:
            continue
        if name.startswith("anon"):
            if pending_var and kind in FUNCTION_TYPES:
                named.append((kind, pending_var))
            pending_var = None
            continue
        named.append((kind, name))
        pending_var = None
    return named


def _ts_signature(sig: dict) -> str:
    params = ", ".join(p.get("name", "") for p in sig.get("parameters", []) if p.get("name"))
    ret = sig.get("return_type")
    return f"({params})" + (f" -> {ret}" if ret else "")
