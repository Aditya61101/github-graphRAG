from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any
import json

from .git_tree import GitFile


@dataclass
class ManifestFile:
    name: str
    size: int


@dataclass
class ManifestDirectory:
    name: str
    path: str
    file_count: int = 0
    total_size: int = 0
    files: list[ManifestFile] = field(default_factory=list)
    directories: list["ManifestDirectory"] = field(default_factory=list)


@dataclass
class RepositoryManifest:
    repository: str
    commit: str
    file_count: int
    total_size: int
    directory_count: int
    root: ManifestDirectory

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            indent=2,
        )


def build_repository_manifest(
    repo_name: str,
    commit: str,
    files: list[GitFile],
) -> RepositoryManifest:

    root = ManifestDirectory(
        name="",
        path="",
    )

    # Maps canonical directory paths to their manifest nodes.
    directories: dict[str, ManifestDirectory] = {
        "": root,
    }

    total_size = 0

    for file in files:
        path = PurePosixPath(file.path)

        current_path = ""
        current_directory = root

        # --------------------------------------------------
        # Build / reuse directory nodes.
        # --------------------------------------------------

        for directory_name in path.parts[:-1]:
            current_path = (
                f"{current_path}/{directory_name}"
                if current_path
                else directory_name
            )

            directory = directories.get(current_path)

            if directory is None:
                directory = ManifestDirectory(
                    name=directory_name,
                    path=current_path,
                )

                directories[current_path] = directory
                current_directory.directories.append(directory)

            current_directory = directory

        # --------------------------------------------------
        # Add file to its immediate directory.
        # --------------------------------------------------

        current_directory.files.append(
            ManifestFile(
                name=path.name,
                size=file.size,
            )
        )

        total_size += file.size

    # ------------------------------------------------------
    # Aggregate file counts / sizes bottom-up.
    # ------------------------------------------------------
    def aggregate(root: ManifestDirectory) -> tuple[int, int]:
        # Post-order traversal with an explicit stack
        stack = [(root, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                file_count = len(node.files)
                total_size = sum(f.size for f in node.files)
                for child in node.directories:
                    file_count += child.file_count
                    total_size += child.total_size
                node.file_count = file_count
                node.total_size = total_size
            else:
                stack.append((node, True))
                for child in node.directories:
                    stack.append((child, False))
        return root.file_count, root.total_size   

    file_count, total_size = aggregate(root)

    return RepositoryManifest(
        repository=repo_name,
        commit=commit,
        file_count=file_count,
        total_size=total_size,
        directory_count=len(directories) - 1,
        root=root,
    )