from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

import httpx

from ai_services.ingestion.rkg.incremental import ChangeKind, FileChange
from ai_services.ingestion.sources.credentials import (
    CredentialProvider,
    GitHubCredential,
    sanitize_sensitive_text,
)
from .interface import (
    CorruptedCloneError,
    PrivateRepositoryUnsupportedError,
    RepositoryAuthenticationError,
    RepositoryMetadata,
    RepositoryNotFoundError,
    RepositoryRef,
    RepositorySnapshot,
    RepositorySource,
    RepositorySourceError,
)

logger = logging.getLogger(__name__)


class GitHubRepositorySource(RepositorySource):
    """GitHub repository provider handling remote metadata, clones, and diffs with user credentials."""

    def __init__(
        self,
        storage_dir: Path | str,
        credential_provider: CredentialProvider | None = None,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.credential_provider = credential_provider
        self.api_base_url = api_base_url.rstrip("/")

    def resolve_ref(self, identifier: str, branch: str | None = None) -> RepositoryRef:
        return RepositoryRef.parse(identifier, branch=branch)

    def _resolve_credential(
        self,
        credential: GitHubCredential | None,
        ref: RepositoryRef | None = None,
    ) -> GitHubCredential | None:
        if credential is not None:
            return credential
        return None

    def _get_api_headers(self, credential: GitHubCredential | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DecisionGuard-GraphRAG",
        }
        if credential and credential.token:
            headers["Authorization"] = f"{credential.token_type} {credential.token}"
        return headers

    async def get_metadata(
        self,
        ref: RepositoryRef,
        credential: GitHubCredential | None = None,
    ) -> RepositoryMetadata:
        cred = self._resolve_credential(credential, ref)
        url = f"{self.api_base_url}/repos/{ref.owner}/{ref.name}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=self._get_api_headers(cred))
            except Exception as exc:
                cleaned_msg = sanitize_sensitive_text(str(exc))
                raise RepositorySourceError(f"Failed to connect to GitHub API: {cleaned_msg}") from None

        if response.status_code == 404:
            raise RepositoryNotFoundError(
                f"Repository '{ref.full_name}' was not found on GitHub."
            )
        if response.status_code in {401, 403}:
            cleaned_err = sanitize_sensitive_text(response.text)
            raise RepositoryAuthenticationError(
                f"Authentication failed for repository '{ref.full_name}': {cleaned_err}"
            )
        if response.status_code != 200:
            cleaned_err = sanitize_sensitive_text(response.text)
            raise RepositorySourceError(
                f"GitHub API returned unexpected status {response.status_code}: {cleaned_err}"
            )

        data = response.json()
        is_private = bool(data.get("private", False)) or data.get("visibility") == "private"
        if is_private:
            raise PrivateRepositoryUnsupportedError(
                f"Repository '{ref.full_name}' is private. "
                "The current DecisionGuard prototype supports public GitHub repositories only."
            )

        default_branch = data.get("default_branch", "main")
        tracked_branch = ref.branch or default_branch
        visibility = data.get("visibility", "public")

        return RepositoryMetadata(
            github_repository_id=str(data["id"]),
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=default_branch,
            tracked_branch=tracked_branch,
            repository_url=data.get("html_url", f"https://github.com/{ref.full_name}"),
            visibility=visibility,
        )

    def _repo_clone_path(self, ref: RepositoryRef) -> Path:
        return self.storage_dir / ref.owner / ref.name

    def _build_git_auth_args(self, credential: GitHubCredential | None) -> list[str]:
        # Use GitHub HTTPS token authentication without putting the token
        # directly into the repository URL.
        if credential and credential.token:
            auth = base64.b64encode(
                f"x-access-token:{credential.token}".encode("utf-8")
            ).decode("ascii")
            return [
                "-c",
                f"http.extraheader=Authorization: Basic {auth}",
            ]
        return []

    def _run_git(
        self,
        args: Sequence[str],
        credential: GitHubCredential | None = None,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git"] + self._build_git_auth_args(credential) + list(args)
        env = os.environ.copy()
        # Never allow Git to invoke interactive credential helpers/browser auth
        # from a background ingestion process.
        env["GIT_TERMINAL_PROMPT"] = "0"

        res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if check and res.returncode != 0:
            cleaned_err = sanitize_sensitive_text(res.stderr.strip())
            raise RepositorySourceError(f"Git command failed: {cleaned_err}")
        return res

    async def prepare_snapshot(
        self,
        ref: RepositoryRef,
        target_commit: str | None = None,
        credential: GitHubCredential | None = None,
    ) -> RepositorySnapshot:
        cred = self._resolve_credential(credential, ref)
        clone_path = self._repo_clone_path(ref)

        if (clone_path / ".git").is_dir():
            try:
                return await self.update_snapshot(ref, target_commit=target_commit, credential=cred)
            except CorruptedCloneError:
                shutil.rmtree(clone_path, ignore_errors=True)

        metadata = await self.get_metadata(ref, credential=cred)
        tracked_branch = ref.branch or metadata.default_branch
        clone_url = f"https://github.com/{metadata.full_name}.git"

        clone_path.parent.mkdir(parents=True, exist_ok=True)

        def _do_clone() -> None:
            clone_args = ["clone"]
            if not target_commit:
                clone_args.extend(["--branch", tracked_branch])
            clone_args.extend([clone_url, str(clone_path)])

            res = self._run_git(clone_args, credential=cred, check=False)
            if res.returncode != 0:
                cleaned_err = sanitize_sensitive_text(res.stderr.strip())
                raise RepositorySourceError(
                    f"git clone failed for {ref.full_name}: {cleaned_err}"
                )

            if target_commit:
                checkout_res = self._run_git(
                    ["checkout", target_commit], credential=cred, cwd=clone_path, check=False
                )
                if checkout_res.returncode != 0:
                    cleaned_err = sanitize_sensitive_text(checkout_res.stderr.strip())
                    raise RepositorySourceError(
                        f"Failed to checkout commit {target_commit}: {cleaned_err}"
                    )

        await asyncio.to_thread(_do_clone)

        commit_sha = self._resolve_head_sha(clone_path)
        ref_with_id = RepositoryRef(
            owner=ref.owner,
            name=ref.name,
            branch=tracked_branch,
            commit_sha=commit_sha,
            github_repo_id=str(metadata.github_repository_id),
        )
        return RepositorySnapshot(
            ref=ref_with_id,
            root_path=clone_path,
            commit_sha=commit_sha,
            branch=tracked_branch,
        )

    async def update_snapshot(
        self,
        ref: RepositoryRef,
        target_commit: str | None = None,
        credential: GitHubCredential | None = None,
    ) -> RepositorySnapshot:
        cred = self._resolve_credential(credential, ref)
        clone_path = self._repo_clone_path(ref)
        if not (clone_path / ".git").is_dir():
            return await self.prepare_snapshot(ref, target_commit=target_commit, credential=cred)

        metadata = await self.get_metadata(ref, credential=cred)
        tracked_branch = ref.branch or metadata.default_branch

        def _do_update() -> None:
            fetch_res = self._run_git(
                ["fetch", "origin", "--prune"], credential=cred, cwd=clone_path, check=False
            )
            if fetch_res.returncode != 0:
                cleaned_err = sanitize_sensitive_text(fetch_res.stderr.strip())
                raise CorruptedCloneError(
                    f"git fetch failed in {clone_path}: {cleaned_err}"
                )

            if target_commit:
                co_res = self._run_git(
                    ["checkout", target_commit], credential=cred, cwd=clone_path, check=False
                )
                if co_res.returncode != 0:
                    cleaned_err = sanitize_sensitive_text(co_res.stderr.strip())
                    raise RepositorySourceError(
                        f"git checkout {target_commit} failed: {cleaned_err}"
                    )
            else:
                co_res = self._run_git(
                    ["checkout", tracked_branch], credential=cred, cwd=clone_path, check=False
                )
                if co_res.returncode != 0:
                    co_res = self._run_git(
                        ["checkout", "-B", tracked_branch, f"origin/{tracked_branch}"],
                        credential=cred,
                        cwd=clone_path,
                        check=False,
                    )
                    if co_res.returncode != 0:
                        cleaned_err = sanitize_sensitive_text(co_res.stderr.strip())
                        raise CorruptedCloneError(
                            f"git checkout {tracked_branch} failed: {cleaned_err}"
                        )
                reset_res = self._run_git(
                    ["reset", "--hard", f"origin/{tracked_branch}"],
                    credential=cred,
                    cwd=clone_path,
                    check=False,
                )
                if reset_res.returncode != 0:
                    cleaned_err = sanitize_sensitive_text(reset_res.stderr.strip())
                    raise CorruptedCloneError(
                        f"git reset failed: {cleaned_err}"
                    )

        await asyncio.to_thread(_do_update)

        commit_sha = self._resolve_head_sha(clone_path)
        ref_with_id = RepositoryRef(
            owner=ref.owner,
            name=ref.name,
            branch=tracked_branch,
            commit_sha=commit_sha,
            github_repo_id=str(metadata.github_repository_id),
        )
        return RepositorySnapshot(
            ref=ref_with_id,
            root_path=clone_path,
            commit_sha=commit_sha,
            branch=tracked_branch,
        )

    def _resolve_head_sha(self, clone_path: Path) -> str:
        res = self._run_git(["rev-parse", "HEAD"], cwd=clone_path, check=True)
        sha = res.stdout.strip()
        if len(sha) != 40:
            raise RepositorySourceError(f"Unexpected commit SHA '{sha}' from git rev-parse HEAD.")
        return sha

    def compute_diff(
        self,
        snapshot: RepositorySnapshot,
        base_commit: str,
        target_commit: str,
    ) -> list[FileChange]:
        if base_commit == target_commit:
            return []

        res = self._run_git(
            ["diff", "--name-status", "-M", base_commit, target_commit],
            cwd=snapshot.root_path,
            check=False,
        )
        if res.returncode != 0:
            cleaned_err = sanitize_sensitive_text(res.stderr.strip())
            raise RepositorySourceError(
                f"git diff failed between {base_commit} and {target_commit}: {cleaned_err}"
            )

        changes: list[FileChange] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t")
            status = parts[0]

            if status.startswith("A"):
                changes.append(FileChange(path=parts[1], kind=ChangeKind.ADDED))
            elif status.startswith("M"):
                changes.append(FileChange(path=parts[1], kind=ChangeKind.MODIFIED))
            elif status.startswith("D"):
                changes.append(FileChange(path=parts[1], kind=ChangeKind.DELETED))
            elif status.startswith("R"):
                old_path = parts[1]
                new_path = parts[2]
                changes.append(FileChange(path=old_path, kind=ChangeKind.DELETED))
                changes.append(FileChange(path=new_path, kind=ChangeKind.ADDED))

        return changes
