from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Repository:
    root: Path
    name: str

    @classmethod
    def from_path(cls, path: str | Path) -> "Repository":
        root = Path(path).resolve()

        if not root.exists():
            raise FileNotFoundError(f"Repository does not exist: {root}")

        if not root.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {root}")

        return cls(
            root=root,
            name=root.name,
        )