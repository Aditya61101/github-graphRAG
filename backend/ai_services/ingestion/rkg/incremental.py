from dataclasses import dataclass
from enum import Enum

class ChangeKind(str, Enum):
    ADDED="added"; MODIFIED="modified"; DELETED="deleted"; UNCHANGED="unchanged"; RENAMED="renamed"

@dataclass(frozen=True)
class FileChange:
    path: str
    kind: ChangeKind
    old_hash: str | None = None
    new_hash: str | None = None

class IncrementalImpactPlanner:
    def affected_paths(self, changes):
        return {c.path for c in changes if c.kind != ChangeKind.UNCHANGED}
