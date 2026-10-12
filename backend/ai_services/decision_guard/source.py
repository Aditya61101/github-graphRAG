import re
from pathlib import Path, PurePosixPath

from ai_services.ingestion.pr.git_reader import PullRequestGitReader

from .ports import SourceBlob


def validate_path(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or PurePosixPath(path).is_absolute()
    ):
        raise ValueError("Expected a repository-relative Git path")
    return path


class GitRevisionSourceReader:
    """Reuse bounded PR Git object/blob reads; never fetch or change refs/worktree."""

    def __init__(self, repository_id: str, root: Path, reader: PullRequestGitReader):
        self.repository_id, self.root, self.reader = (
            repository_id,
            Path(root).resolve(),
            reader,
        )
        if not self.root.is_relative_to(reader.source.storage_dir):
            raise ValueError("Clone path escapes repository storage")

    def read_blob(self, repository_id: str, commit_sha: str, path: str) -> SourceBlob:
        if repository_id != self.repository_id:
            raise ValueError("Source reader repository mismatch")
        validate_path(path)
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise ValueError("Expected an exact commit SHA")
        self.reader.validate_clone(self.root)
        if not self.reader._has_commit(self.root, commit_sha):
            raise ValueError("Exact source commit unavailable")
        result = self.reader._git(self.root, ["ls-tree", "-z", commit_sha, "--", path])
        entries = [entry for entry in result.stdout.split(b"\0") if entry]
        exact = []
        for entry in entries:
            metadata, name = entry.split(b"\t", 1)
            if name.decode("utf-8") == path:
                exact.append(metadata.decode("ascii").split())
        if not exact:
            return SourceBlob(
                repository_id=repository_id,
                commit_sha=commit_sha,
                path=path,
                status="absent",
            )
        if len(exact) != 1 or exact[0][1] not in {"blob", "commit"}:
            raise ValueError("Source path does not identify a single file")
        mode, _, oid = exact[0]
        text, status, _ = self.reader.get_file_content(
            self.root, oid, mode, self.reader.settings.max_file_bytes
        )
        return SourceBlob(
            repository_id=repository_id,
            commit_sha=commit_sha,
            path=path,
            status=status,
            text=text,
        )
