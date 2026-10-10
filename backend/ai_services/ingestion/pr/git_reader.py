from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from ai_services.ingestion.sources.github import GitHubRepositorySource, GitOutputLimitError
from ai_services.ingestion.sources.credentials import GitHubCredential
from .models import ChangedFile, PullRequestEvent, PullRequestIngestionError, SupersededRevisionError
from .settings import PullRequestSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Entry:
    path: str
    previous_path: str | None
    status: str
    base_mode: str
    head_mode: str
    base_oid: str
    head_oid: str


class PullRequestGitReader:
    """Synchronous object-only reader; the service runs it in a worker thread."""
    DIFF_OPTIONS = ['--no-ext-diff', '--no-textconv', '--diff-algorithm=myers',
                    '-M50%', '-C50%', '--find-copies-harder', '-l1000']

    def __init__(self, source: GitHubRepositorySource, settings: PullRequestSettings):
        self.source, self.settings = source, settings

    def _git(self, root, args, *, credential=None, check=True, limit=None):
        return self.source._run_git(['--no-pager', '--literal-pathspecs', *args],
            cwd=root, credential=credential, check=check, binary=True,
            max_output_bytes=self.settings.max_metadata_bytes if limit is None else limit,
            timeout_seconds=self.settings.git_timeout_seconds)

    def validate_clone(self, root: Path):
        if not root.is_dir() or not (root / '.git').is_dir():
            raise PullRequestIngestionError('Accepted repository clone unavailable; ingest/sync the repository first')
        result = self._git(root, ['rev-parse', '--show-toplevel'])
        top = Path(result.stdout.decode().strip()).resolve()
        if top != root.resolve():
            raise PullRequestIngestionError('Local clone does not match the tracked repository root')

    def _has_commit(self, root, sha):
        return self._git(root, ['cat-file', '-e', sha + '^{commit}'], check=False).returncode == 0

    def fetch_pr_revision(self, root, event: PullRequestEvent, credential: GitHubCredential, remote_url: str):
        self.validate_clone(root)
        if not self._has_commit(root, event.head_sha):
            ref = f'refs/decisionguard/pr/{event.pull_request_number}/head'
            self._git(root, ['fetch', '--no-tags', '--no-write-fetch-head', remote_url,
                f'+refs/pull/{event.pull_request_number}/head:{ref}'], credential=credential)
            if not self._has_commit(root, event.head_sha):
                actual = self._git(root, ['rev-parse', '--verify', ref + '^{commit}']).stdout.decode().strip()
                if actual != event.head_sha:
                    raise SupersededRevisionError('PR ref advanced and the exact webhook head is unavailable')
                raise PullRequestIngestionError('Expected PR head commit is unavailable')
        if not self._has_commit(root, event.base_sha):
            self._git(root, ['fetch', '--no-tags', '--no-write-fetch-head', remote_url,
                f'+{event.base_sha}:refs/decisionguard/pr/{event.pull_request_number}/base'], credential=credential)
        if not self._has_commit(root, event.base_sha) or not self._has_commit(root, event.head_sha):
            raise PullRequestIngestionError('Exact webhook commits could not be retrieved')
        logger.info('PR Git objects available pr=%s base=%s head=%s',
                    event.pull_request_number, event.base_sha, event.head_sha)

    def resolve_merge_base(self, root, event):
        # No indefinite deepen loop: shallow or disconnected history fails clearly.
        result = self._git(root, ['merge-base', '--all', event.base_sha, event.head_sha], check=False)
        values = result.stdout.decode().splitlines()
        if result.returncode or len(values) != 1:
            raise PullRequestIngestionError('Unique merge base unavailable (shallow, disconnected, or ambiguous history); sync a full clone')
        return values[0]

    def get_changed_files(self, root, base, head):
        raw = self._git(root, ['diff', *self.DIFF_OPTIONS, '--raw', '--no-abbrev', '-z', base, head, '--']).stdout
        parts = iter(raw.split(b'\0')[:-1])
        entries = []
        statuses = {'A': 'added', 'M': 'modified', 'D': 'removed', 'R': 'renamed', 'C': 'copied', 'T': 'type_changed'}
        try:
            for header in parts:
                fields = header.decode('ascii').split()
                if len(fields) != 5 or not fields[0].startswith(':') or fields[4][0] not in statuses:
                    raise PullRequestIngestionError('Unsupported Git change status')
                old_path = next(parts).decode('utf-8')
                code = fields[4][0]
                new_path = next(parts).decode('utf-8') if code in {'R', 'C'} else old_path
                entries.append(_Entry(new_path, old_path if code in {'R', 'C'} else None,
                    statuses[code], fields[0][1:], fields[1], fields[2], fields[3]))
                if len(entries) > self.settings.max_changed_files:
                    raise PullRequestIngestionError('PR exceeds configured changed-file limit')
        except (StopIteration, UnicodeError) as exc:
            raise PullRequestIngestionError('Invalid or non-UTF-8 Git diff paths') from exc
        return entries

    def _statistics(self, root, base, head):
        raw = self._git(root, ['diff', *self.DIFF_OPTIONS, '--numstat', '-z', base, head, '--']).stdout
        parts = iter(raw.split(b'\0')[:-1])
        output = {}
        try:
            for record in parts:
                added, deleted, path = record.split(b'\t', 2)
                if not path:
                    next(parts)  # old path of a rename/copy
                    path = next(parts)
                output[path.decode('utf-8')] = (None, None) if added == b'-' or deleted == b'-' else (int(added), int(deleted))
        except (StopIteration, ValueError, UnicodeError) as exc:
            raise PullRequestIngestionError('Invalid Git statistics output') from exc
        return output

    def get_file_content(self, root, oid, mode, budget):
        """Classify this blob independently of Git's binary comparison status."""
        if mode == '000000':
            return None, 'absent', None
        if mode == '160000':
            return None, 'submodule', None
        size = int(self._git(root, ['cat-file', '-s', oid], limit=100).stdout)
        if mode == '120000':
            return None, 'symlink', size
        if mode not in {'100644', '100755'}:
            raise PullRequestIngestionError('Unsupported Git tree mode')
        if size > self.settings.max_file_bytes:
            return None, 'oversized', size
        if size > budget:
            return None, 'budget_exceeded', size
        content = self._git(root, ['cat-file', 'blob', oid], limit=self.settings.max_file_bytes).stdout
        if b'\0' in content:
            return None, 'binary', size
        try:
            return content.decode('utf-8'), 'complete', size
        except UnicodeDecodeError:
            return None, 'non_utf8', size

    def build_file_changes(self, root, base, head):
        entries = self.get_changed_files(root, base, head)
        statistics = self._statistics(root, base, head)
        budget = self.settings.max_total_text_bytes
        files = []
        for entry in entries:
            if entry.path not in statistics:
                raise PullRequestIngestionError('Git status/statistics mismatch')
            additions, deletions = statistics[entry.path]
            # Numstat describes the comparison, not either individual blob.
            diff_is_binary = additions is None
            base_text, base_status, base_size = self.get_file_content(root, entry.base_oid, entry.base_mode, budget)
            budget -= len(base_text.encode()) if base_text is not None else 0
            head_text, head_status, head_size = self.get_file_content(root, entry.head_oid, entry.head_mode, budget)
            budget -= len(head_text.encode()) if head_text is not None else 0
            statuses = {base_status, head_status} - {'absent'}
            patch = None
            if diff_is_binary or statuses & {'binary'}:
                patch_status = 'binary'
            elif statuses & {'symlink', 'submodule'}:
                patch_status = 'non_regular'
            elif statuses & {'oversized'}:
                patch_status = 'oversized'
            elif statuses & {'non_utf8'}:
                patch_status = 'non_utf8'
            elif statuses & {'budget_exceeded'} or budget <= 0:
                patch_status = 'budget_exceeded'
            else:
                try:
                    paths = list(dict.fromkeys([entry.previous_path or entry.path, entry.path]))
                    # Limit pathspecs literally; '-' or ':' in a filename is not an option.
                    # A modified/reused old path must not leak into a copy or
                    # rename's patch. Paths remain explicit on ChangedFile.
                    args = (['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--unified=3',
                             entry.base_oid, entry.head_oid] if entry.status in {'copied', 'renamed'} else
                            ['diff', *self.DIFF_OPTIONS, '--no-color', '--src-prefix=a/',
                             '--dst-prefix=b/', '--unified=3', base, head, '--', *paths])
                    raw = self._git(root, args,
                        limit=min(self.settings.max_patch_bytes, budget)).stdout
                    patch = raw.decode('utf-8')
                    budget -= len(raw)
                    patch_status = 'complete'
                except GitOutputLimitError:
                    patch_status = 'oversized' if self.settings.max_patch_bytes <= budget else 'budget_exceeded'
                except UnicodeDecodeError:
                    patch_status = 'non_utf8'
            files.append(ChangedFile(path=entry.path, previous_path=entry.previous_path, status=entry.status,
                additions=additions, deletions=deletions, changes=None if diff_is_binary else additions + deletions,
                patch=patch, patch_status=patch_status, base_content=base_text, head_content=head_text,
                base_content_status=base_status, head_content_status=head_status,
                base_size_bytes=base_size, head_size_bytes=head_size,
                base_mode=None if entry.base_mode == '000000' else entry.base_mode,
                head_mode=None if entry.head_mode == '000000' else entry.head_mode))
        return files

    def read_revision(self, root, event, credential, remote_url):
        self.fetch_pr_revision(root, event, credential, remote_url)
        merge_base = self.resolve_merge_base(root, event)
        logger.info('PR merge base resolved pr=%s merge_base=%s', event.pull_request_number, merge_base)
        return merge_base, self.build_file_changes(root, merge_base, event.head_sha)
