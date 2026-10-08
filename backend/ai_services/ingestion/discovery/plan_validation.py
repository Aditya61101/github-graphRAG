"""Manifest coverage and canonical repository-relative planner paths."""
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath

from ai_services.models.ingestion_plan import IngestionPlan


class PlannerCoverageError(ValueError):
    def __init__(self, *, missing=(), duplicates=(), unknown=(), invalid=()):
        self.details = {
            "missing": sorted(missing), "duplicates": sorted(duplicates),
            "unknown": sorted(unknown), "invalid": sorted(invalid),
        }
        super().__init__(f"Invalid ingestion plan coverage: {self.details}")


def validate_repository_relative_path(path: str) -> None:
    """Git manifest paths use canonical POSIX separators on every host OS."""
    if (
        not isinstance(path, str) or not path or "\x00" in path or "\\" in path
        or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or path == "~" or path.startswith("~/")
    ):
        raise ValueError(f"Not a canonical repository-relative path: {path!r}")


def manifest_paths(manifest: dict) -> set[str]:
    paths = []
    pending = [manifest["root"]]
    while pending:
        directory = pending.pop()
        prefix = directory["path"]
        for item in directory["files"]:
            path = f"{prefix}/{item['name']}" if prefix else item["name"]
            validate_repository_relative_path(path)
            paths.append(path)
        pending.extend(directory["directories"])
    if len(paths) != len(set(paths)) or len(paths) != manifest["file_count"]:
        raise ValueError("Manifest file count or path uniqueness is inconsistent")
    return set(paths)


def validate_ingestion_plan(plan: IngestionPlan, manifest: dict) -> None:
    expected = manifest_paths(manifest)
    counts = Counter(item.path for item in plan.files)
    invalid = []
    for path in counts:
        try:
            validate_repository_relative_path(path)
        except ValueError:
            invalid.append(path)
    details = {
        "missing": expected - counts.keys(),
        "duplicates": [path for path, count in counts.items() if count != 1],
        "unknown": counts.keys() - expected,
        "invalid": invalid,
    }
    if any(details.values()):
        raise PlannerCoverageError(**details)


def resolve_planned_file(root: Path, path: str) -> Path:
    validate_repository_relative_path(path)
    root = root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Planned path resolves outside repository root: {path!r}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Planned repository file does not exist: {path!r}")
    return resolved
