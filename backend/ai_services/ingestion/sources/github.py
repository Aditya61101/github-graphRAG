from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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


class GitOutputLimitError(RepositorySourceError):
    """Git output exceeded an explicit bound; no truncated result is returned."""


class GitHubRepositorySource(RepositorySource):
    """GitHub metadata, clones, and diffs using explicitly supplied credentials."""

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
                cleaned_msg = self._safe_git_error(str(exc), cred)
                raise RepositorySourceError(f"Failed to connect to GitHub API: {cleaned_msg}") from None

        if response.status_code == 404:
            raise RepositoryNotFoundError(
                f"Repository '{ref.full_name}' was not found on GitHub."
            )
        if response.status_code in {401, 403}:
            cleaned_err = self._safe_git_error(response.text, cred)
            raise RepositoryAuthenticationError(
                f"Authentication failed for repository '{ref.full_name}': {cleaned_err}"
            )
        if response.status_code != 200:
            cleaned_err = self._safe_git_error(response.text, cred)
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

    def _run_git(
        self,
        args: Sequence[str],
        credential: GitHubCredential | None = None,
        cwd: Path | None = None,
        check: bool = True,
        binary: bool = False,
        max_output_bytes: int | None = None,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        cmd = ["git"] + list(args)
        env = os.environ.copy()
        # Never allow Git to invoke interactive credential helpers/browser auth
        # from a background ingestion process.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS"] = ""
        # Reset inherited credentials: never fall back to a developer's user
        # token or send a previously configured Authorization header.
        env['GIT_CONFIG_COUNT'] = '2'
        env['GIT_CONFIG_KEY_0'] = 'credential.helper'
        env['GIT_CONFIG_VALUE_0'] = ''
        env['GIT_CONFIG_KEY_1'] = 'http.extraheader'
        env['GIT_CONFIG_VALUE_1'] = ''
        if credential and credential.token:
            auth = base64.b64encode(f'x-access-token:{credential.token}'.encode()).decode()
            env['GIT_CONFIG_COUNT'] = '3'
            env['GIT_CONFIG_KEY_2'] = 'http.extraheader'
            env['GIT_CONFIG_VALUE_2'] = f'Authorization: Basic {auth}'

        if max_output_bytes is not None:
            # Spool bounded-reader commands to disk rather than materializing
            # potentially huge diffs in memory. Content is size-checked first.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                try:
                    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                        stdout=output, stderr=errors, env=env, timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    raise RepositorySourceError('Git operation timed out') from None
                size = output.tell()
                output.seek(0)
                errors.seek(0)
                stderr = errors.read(8192).decode('utf-8', errors='replace')
                if check and result.returncode:
                    if self._is_auth_error(stderr):
                        raise RepositoryAuthenticationError('GitHub Git credentials expired or access denied')
                    raise RepositorySourceError(self._safe_git_error(stderr, credential))
                if size > max_output_bytes:
                    raise GitOutputLimitError('Git output exceeds the configured limit')
                stdout = output.read()
                return subprocess.CompletedProcess(cmd, result.returncode,
                    stdout if binary else stdout.decode('utf-8', errors='surrogateescape'),
                    stderr.encode() if binary else stderr)

        res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=not binary,
            check=False,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            env=env,
            timeout=timeout_seconds,
        )
        if check and res.returncode != 0:
            stderr = res.stderr.decode('utf-8', errors='replace') if binary else res.stderr
            cleaned_err = self._safe_git_error(stderr.strip(), credential)
            if self._is_auth_error(stderr):
                raise RepositoryAuthenticationError('GitHub Git credentials expired or access denied')
            raise RepositorySourceError(f"Git command failed: {cleaned_err}")
        return res

    @staticmethod
    def _is_auth_error(text: str) -> bool:
        return any(value in text.lower() for value in ('authentication failed', '401', '403', 'could not read username', 'access denied'))

    @staticmethod
    def _safe_git_error(text: str, credential) -> str:
        if credential:
            text = text.replace(credential.token, '[REDACTED]')
            encoded = base64.b64encode(f'x-access-token:{credential.token}'.encode()).decode()
            text = text.replace(encoded, '[REDACTED]')
        return sanitize_sensitive_text(text)

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
                cleaned_err = self._safe_git_error(res.stderr.strip(), cred)
                if self._is_auth_error(res.stderr):
                    raise RepositoryAuthenticationError('GitHub Git credentials expired or access denied')
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
                cleaned_err = self._safe_git_error(fetch_res.stderr.strip(), cred)
                if self._is_auth_error(fetch_res.stderr):
                    raise RepositoryAuthenticationError('GitHub Git credentials expired or access denied')
                raise RepositorySourceError(
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
