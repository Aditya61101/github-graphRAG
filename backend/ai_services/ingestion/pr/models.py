from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

PositiveID = Annotated[StrictInt, Field(gt=0)]
SHA = Annotated[str, Field(pattern=r'^[0-9a-fA-F]{40}$')]
ContentStatus = Literal['complete', 'absent', 'binary', 'oversized', 'non_utf8', 'symlink', 'submodule', 'budget_exceeded']


class PullRequestEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    installation_id: PositiveID
    github_repository_id: PositiveID
    repository_full_name: str = Field(min_length=3)
    repository_private: bool
    pull_request_number: PositiveID
    title: str = Field(min_length=1)
    body: str | None
    base_branch: str = Field(min_length=1)
    head_branch: str = Field(min_length=1)
    base_sha: SHA
    head_sha: SHA
    base_repository_id: PositiveID
    head_repository_id: PositiveID | None = None
    head_repository_full_name: str | None = None
    source_event: Literal['opened', 'synchronize', 'reopened']
    delivery_id: str | None = Field(default=None, max_length=255)

    @field_validator('base_sha', 'head_sha')
    @classmethod
    def normalize_sha(cls, value):
        return value.lower()

    @field_validator('repository_full_name', 'head_repository_full_name')
    @classmethod
    def validate_name(cls, value):
        if value is not None and (len(value.split('/')) != 2 or
                any(part in {'', '.', '..'} for part in value.split('/')) or
                any(character in value for character in '\\\x00\r\n')):
            raise ValueError('Invalid GitHub repository full name')
        return value

    @classmethod
    def from_webhook(cls, payload: dict, delivery_id: str | None):
        try:
            pr = payload['pull_request']
            base, head = pr['base'], pr['head']
            repository = payload['repository']
            head_repo = head.get('repo')
            return cls(installation_id=payload['installation']['id'],
                github_repository_id=repository['id'], repository_full_name=repository['full_name'],
                repository_private=repository['private'], pull_request_number=pr['number'],
                title=pr['title'], body=pr.get('body'), base_branch=base['ref'], head_branch=head['ref'],
                base_sha=base['sha'], head_sha=head['sha'], base_repository_id=base['repo']['id'],
                head_repository_id=head_repo['id'] if head_repo else None,
                head_repository_full_name=head_repo['full_name'] if head_repo else None,
                source_event=payload['action'], delivery_id=delivery_id)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Incomplete pull request webhook metadata') from exc

    @property
    def revision_key(self):
        value = f'{self.github_repository_id}:{self.pull_request_number}:{self.base_sha}:{self.head_sha}'
        return hashlib.sha256(value.encode()).hexdigest()


class ChangedFile(BaseModel):
    path: str
    previous_path: str | None = None
    status: Literal['added', 'modified', 'removed', 'renamed', 'copied', 'type_changed']
    additions: int | None
    deletions: int | None
    changes: int | None
    patch: str | None = None
    patch_status: Literal['complete', 'binary', 'oversized', 'non_utf8', 'non_regular', 'budget_exceeded']
    base_content: str | None = None
    head_content: str | None = None
    base_content_status: ContentStatus
    head_content_status: ContentStatus
    base_size_bytes: int | None = None
    head_size_bytes: int | None = None
    base_mode: str | None = None
    head_mode: str | None = None


class PullRequestChangeSet(PullRequestEvent):
    repository_id: str
    merge_base_sha: SHA
    files: list[ChangedFile]
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PullRequestIngestionError(Exception):
    pass


class SupersededRevisionError(PullRequestIngestionError):
    pass
