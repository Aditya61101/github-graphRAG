# git_tree.py

from dataclasses import dataclass
from pathlib import Path
import subprocess

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

@dataclass(frozen=True)
class GitFile:
    path: str
    mode: str
    object_type: str
    object_id: str
    size: int
    
def _run_git(
    repo_root: Path,
    *args: str,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input,
        capture_output=True,
        check=True,
    )

def _get_git_tree(repo_root: Path) -> list[bytes]:
    result = _run_git(
        repo_root,
        "ls-tree",
        "-r",
        "-l",
        "-z",
        "HEAD",
    )

    return [
        entry
        for entry in result.stdout.split(b"\0")
        if entry
    ]

def _get_ignored_paths(
    repo_root: Path,
    entries: list[bytes],
) -> set[str]:

    if not entries:
        return set()

    paths_input = b"\0".join(
        entry.split(b"\t", 1)[1]
        for entry in entries
    ) + b"\0"

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        input=paths_input,
        capture_output=True,
        check=False,
    )

    if result.returncode == 128:
        raise RuntimeError(
            "git check-ignore failed: "
            + result.stderr.decode(errors="replace")
        )

    return {
        path.decode()
        for path in result.stdout.split(b"\0")
        if path
    }

def get_repository_files(repo_root: Path) -> list[GitFile]:
    """
    Return tracked files that are not matched by the repository's
    current .gitignore rules.
    """

    entries = _get_git_tree(repo_root)

    if not entries:
        return []

    ignored_paths = _get_ignored_paths(repo_root, entries)

    files: list[GitFile] = []

    for entry in entries:
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode()

        if path in ignored_paths:
            continue

        mode, object_type, object_id, size = (
            metadata.decode().split()
        )
        
        # Skip non-blob entries (submodules, etc.)
        if object_type != "blob":
            continue
        
        size = int(size)
        # -----------------------------------------
        # 2. File-size safety limit
        # -----------------------------------------
        if size > MAX_FILE_SIZE:
            continue

        files.append(
            GitFile(
                path=path,
                mode=mode,
                object_type=object_type,
                object_id=object_id,
                size=size,
            )
        )

    return files

def get_head_commit(repo_root: Path) -> str:
    result = _run_git(
        repo_root,
        "rev-parse",
        "HEAD",
    )

    return result.stdout.decode().strip()