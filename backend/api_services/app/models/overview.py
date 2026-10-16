from typing import Literal

from pydantic import BaseModel, Field

from api_services.app.models.repository import RepositoryResponse
from api_services.app.models.pull_requests import (
    DecisionRisk,
    RecentReview,
    RiskBreakdown,
)


class Availability(BaseModel):
    status: Literal["available", "unavailable", "error"]
    reason: Literal["engine_not_implemented", "graph_query_failed"] | None = None


class OverviewMetric(BaseModel):
    availability: Availability
    value: int | None = Field(default=None, ge=0)


class OverviewKPIs(BaseModel):
    architecture_decisions: OverviewMetric
    decisions_at_risk: OverviewMetric
    prs_analyzed: OverviewMetric
    architectural_warnings: OverviewMetric


class OverviewSection(BaseModel):
    availability: Availability
    items: list[RecentReview] = Field(default_factory=list)


class ReviewActivity(OverviewSection):
    window_days: Literal[7] = 7


class DecisionsAttention(BaseModel):
    availability: Availability
    items: list[DecisionRisk] = Field(default_factory=list)
    total: int = 0


class OverviewPermissions(BaseModel):
    can_view_settings: bool
    can_manage_source_documents: bool = False


class RepositoryOverviewResponse(BaseModel):
    repository: RepositoryResponse
    permissions: OverviewPermissions
    decision_count_basis: Literal["distinct_persisted_nodes"] = (
        "distinct_persisted_nodes"
    )
    kpis: OverviewKPIs
    review_activity: ReviewActivity
    decisions_needing_attention: DecisionsAttention
    recent_pr_reviews: OverviewSection
    risk_breakdown: RiskBreakdown = Field(default_factory=RiskBreakdown)
