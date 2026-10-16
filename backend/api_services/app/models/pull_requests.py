"""Stage 4.4 read contracts. Timestamps are UTC Unix seconds."""

from typing import Literal

from pydantic import BaseModel, Field
from api_services.app.models.knowledge import KnowledgeItem

from ai_services.decision_guard.evaluation.models import (
    EvaluationCoverage,
    EvaluationResult,
)


class PublicationResponse(BaseModel):
    status: str
    head_sha: str | None = None
    check_run_id: str | None = None
    conclusion: str | None = None
    attempts: int
    updated_at: float | None = None
    published_at: float | None = None
    error_code: str | None = None


class EvaluationSummary(BaseModel):
    job_id: str
    revision_key: str
    base_sha: str
    head_sha: str
    status: str
    stage: str
    created_at: float
    evaluated_at: float | None
    outcome: str | None
    execution: str | None
    coverage: EvaluationCoverage | None
    confirmed_conflicts: int
    review_required_findings: int
    rejected_findings: int
    publication: PublicationResponse


class PullRequestResponse(BaseModel):
    repository_id: str
    repository_full_name: str
    github_url: str
    pr_number: int
    title: str | None
    author: str | None
    base_branch: str | None
    head_branch: str | None
    base_sha: str | None
    head_sha: str | None
    state: Literal["open", "closed", "merged", "unknown"]
    observed_at: float | None
    github_updated_at: float | None
    metadata_freshness: str
    freshness: Literal["CURRENT_OBSERVED", "STALE", "UNKNOWN"]
    evaluation: EvaluationSummary | None


class PullRequestPage(BaseModel):
    items: list[PullRequestResponse]
    total: int
    limit: int
    offset: int


class GoverningRule(BaseModel):
    finding_id: str
    rule_id: str
    decision_id: str | None
    requirement_kind: str | None
    adr_ids: list[str]
    classification: str


class PullRequestDetail(BaseModel):
    pull_request: PullRequestResponse
    result: EvaluationResult | None
    deferred: list[dict]
    governing_rules: list[GoverningRule]
    history: list[EvaluationSummary]
    history_total: int
    limit: int
    offset: int


class RiskBreakdown(BaseModel):
    confirmed_decisions: int = 0
    potential_decisions: int = 0
    unmapped_requirements: int = 0
    confirmed_warnings: int = 0
    potential_warnings: int = 0
    uncertain_units: int = 0


class RecentReview(EvaluationSummary):
    pr_number: int
    state: str
    freshness: str
    title: str | None = None
    observed_at: float | None = None


class ReviewSection(BaseModel):
    items: list[RecentReview] = Field(default_factory=list)


class DecisionRisk(BaseModel):
    identity: str
    kind: Literal["DECISION", "REQUIREMENT"]
    classification: Literal["CONFIRMED", "POTENTIAL"]
    pr_numbers: list[int]
    finding_ids: list[str]
    adr_ids: list[str]
    knowledge: KnowledgeItem | None = None
    knowledge_resolution: Literal['RESOLVED', 'NOT_FOUND', 'UNAVAILABLE'] = 'UNAVAILABLE'
