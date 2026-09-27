"""
Stages 0-1: Discover files and classify them with simple rules (no LLM).

Each file gets a `sha`: the git blob SHA when the folder is a git repo
(so it matches `git ls-tree`), otherwise a SHA-1 of the file content.
Stage "sync" compares this SHA with the one stored in Neo4j to find
added / modified / deleted files.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

CODE_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
}

DOC_KINDS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".rst": "text",
    ".txt": "text",
    ".pptx": "slides",
    ".docx": "word",
}

SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
    ".git", ".idea", ".vscode", "site-packages", ".mypy_cache", ".pytest_cache",
}

# .txt files are only treated as docs when they look like documentation.
TEXT_DOC_HINTS = ("readme", "doc", "guide", "notes", "design", "architecture")


@dataclass(frozen=True)
class RepoFile:
    path: str          # repo-relative, POSIX style
    sha: str
    kind: str          # "code" | "doc"
    language: str      # python / markdown / slides / ...

    @property
    def is_test(self) -> bool:
        p = PurePosixPath(self.path)
        return (
            "tests" in p.parts
            or "test" in p.parts
            or p.name.startswith("test_")
            or p.name.endswith("_test.py")
        )


def classify(path: str) -> tuple[str, str] | None:
    """Return (kind, language) or None when the file should be skipped."""
    p = PurePosixPath(path)
    if any(part in SKIP_DIRS for part in p.parts):
        return None
    suffix = p.suffix.lower()
    if suffix in CODE_LANGUAGES:
        return "code", CODE_LANGUAGES[suffix]
    if suffix in DOC_KINDS:
        if suffix == ".txt" and not any(h in path.lower() for h in TEXT_DOC_HINTS):
            return None
        return "doc", DOC_KINDS[suffix]
    return None


def discover(repo_root: Path) -> list[RepoFile]:
    repo_root = Path(repo_root).resolve()
    entries = _git_files(repo_root) if (repo_root / ".git").exists() else None
    if entries is None:
        entries = _walk_files(repo_root)

    files = []
    for path, sha, size in entries:
        if size > MAX_FILE_SIZE:
            continue
        kind_lang = classify(path)
        if kind_lang:
            files.append(RepoFile(path=path, sha=sha, kind=kind_lang[0], language=kind_lang[1]))
    return sorted(files, key=lambda f: f.path)


def head_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, check=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ---------------------------------------------------------------- helpers

def _git_files(repo_root: Path) -> list[tuple[str, str, int]] | None:
    """Tracked files at HEAD: (path, blob_sha, size). Honors .gitignore."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-r", "-l", "-z", "HEAD"],
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None  # e.g. a repo with no commits yet

    result = []
    for entry in filter(None, out.split(b"\0")):
        meta, raw_path = entry.split(b"\t", 1)
        _mode, obj_type, sha, size = meta.decode().split()
        if obj_type != "blob" or size == "-":
            continue
        result.append((raw_path.decode(), sha, int(size)))
    return result


def _walk_files(repo_root: Path) -> list[tuple[str, str, int]]:
    result = []
    for file in repo_root.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(repo_root).as_posix()
        if any(part in SKIP_DIRS for part in PurePosixPath(rel).parts):
            continue
        data = file.read_bytes()
        result.append((rel, hashlib.sha1(data).hexdigest(), len(data)))
    return result
