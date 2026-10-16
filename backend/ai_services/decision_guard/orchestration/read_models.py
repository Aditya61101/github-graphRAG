"""Small relational projections; original validated results remain canonical."""

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, String, Text

from ai_services.ingestion.persistence.models import Base


class PullRequestState(Base):
    __tablename__ = "dg_pull_requests"
    repository_id = Column(
        String(64), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    pr_number = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    author = Column(String(255))
    base_branch = Column(Text, nullable=False)
    head_branch = Column(Text, nullable=False)
    base_sha = Column(String(40), nullable=False)
    head_sha = Column(String(40), nullable=False)
    state = Column(String(16), nullable=False)
    github_updated_at = Column(Float, nullable=False)
    observed_at = Column(Float, nullable=False)
    freshness = Column(String(16), nullable=False)
    __table_args__ = (Index("ix_dg_pr_state", "repository_id", "state"),)


class EvaluationProjection(Base):
    __tablename__ = "dg_evaluation_projections"
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    evaluation_id = Column(String(64), nullable=False)
    outcome = Column(String(32), nullable=False)
    execution = Column(String(16), nullable=False)
    coverage_json = Column(Text, nullable=False)
    confirmed = Column(Integer, nullable=False)
    review_required = Column(Integer, nullable=False)
    rejected = Column(Integer, nullable=False)
    evaluated_at = Column(Float, nullable=False)


class FindingRule(Base):
    __tablename__ = "dg_finding_rules"
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    finding_id = Column(String(64), primary_key=True)
    rule_id = Column(String(255), nullable=False)
    decision_id = Column(String(255))
    requirement_kind = Column(String(16))
    adr_ids_json = Column(Text, nullable=False)
    classification = Column(String(16), nullable=False)


class CheckPublication(Base):
    __tablename__ = "dg_check_publications"
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    head_sha = Column(String(40), nullable=False)
    evaluation_id = Column(String(64))
    check_run_id = Column(String(32))
    status = Column(String(32), nullable=False)
    conclusion = Column(String(32))
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(Float, nullable=False)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    published_at = Column(Float)
    error_code = Column(String(100))
    lease_token = Column(String(64))
    lease_until = Column(Float)
    # Persisted before POST. An ambiguous POST is reconciled, never blindly repeated.
    create_started_at = Column(Float)
    __table_args__ = (Index("ix_dg_check_due", "status", "next_attempt_at"),)


class PullRequestSyncIntent(Base):
    __tablename__ = "dg_pr_sync_outbox"
    repository_id = Column(
        String(64), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    pr_number = Column(Integer, primary_key=True)
    requested_at = Column(Float, nullable=False)
    next_attempt_at = Column(Float, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    error_code = Column(String(100))


TABLES = [
    PullRequestState.__table__,
    EvaluationProjection.__table__,
    FindingRule.__table__,
    CheckPublication.__table__,
    PullRequestSyncIntent.__table__,
]
