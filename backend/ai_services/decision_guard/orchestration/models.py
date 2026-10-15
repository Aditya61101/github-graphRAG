"""Portable SQLAlchemy tables for the independently versioned Stage 4.3 schema."""

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from ai_services.ingestion.persistence.models import Base


class EvaluationJob(Base):
    __tablename__ = "pr_evaluation_jobs"
    id = Column(String(64), primary_key=True)
    repository_id = Column(
        String(64), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    pr_number = Column(Integer, nullable=False)
    revision_key = Column(String(64), nullable=False)
    base_sha = Column(String(40), nullable=False)
    head_sha = Column(String(40), nullable=False)
    event_json = Column(Text, nullable=False)
    settings_json = Column(Text, nullable=False)
    status = Column(String(32), nullable=False)
    stage = Column(String(32), nullable=False, default="QUEUED")
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    lease_token = Column(String(64))
    lease_until = Column(Float)
    next_attempt_at = Column(Float, nullable=False)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    error_code = Column(String(100))
    error_message = Column(String(500))
    changes_json = Column(Text)
    context_json = Column(Text)
    result_json = Column(Text)
    deferred_json = Column(Text, nullable=False, default="[]")
    provider_attempts = Column(Integer, nullable=False, default=0)
    charged_tokens = Column(Integer, nullable=False, default=0)
    charged_cost = Column(Float, nullable=False, default=0)
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "pr_number",
            "revision_key",
            name="uq_pr_evaluation_revision",
        ),
        Index("ix_pr_evaluation_due", "status", "next_attempt_at", "lease_until"),
    )


class PullRequestEvaluationHead(Base):
    __tablename__ = "pr_evaluation_heads"
    repository_id = Column(
        String(64), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    pr_number = Column(Integer, primary_key=True)
    latest_job_id = Column(String(64), nullable=False)
    current_result_job_id = Column(String(64))


class WebhookDelivery(Base):
    __tablename__ = "pr_evaluation_deliveries"
    delivery_id = Column(String(255), primary_key=True)
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    received_at = Column(Float, nullable=False)


class DispatchIntent(Base):
    __tablename__ = "pr_evaluation_outbox"
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    next_dispatch_at = Column(Float, nullable=False)
    dispatch_count = Column(Integer, nullable=False, default=0)


class EvaluationAttempt(Base):
    __tablename__ = "pr_evaluation_attempts"
    token = Column(String(64), primary_key=True)
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    number = Column(Integer, nullable=False)
    started_at = Column(Float, nullable=False)
    finished_at = Column(Float)
    status = Column(String(32), nullable=False)
    error_code = Column(String(100))


class ModelPacket(Base):
    __tablename__ = "pr_evaluation_packets"
    job_id = Column(
        String(64),
        ForeignKey("pr_evaluation_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    packet_key = Column(String(64), primary_key=True)
    phase = Column(String(16), nullable=False)
    state = Column(String(32), nullable=False)
    request_json = Column(Text, nullable=False)
    reply_json = Column(Text)
    lease_token = Column(String(64), nullable=False)
    reserved_tokens = Column(Integer, nullable=False)
    reserved_cost = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)


TABLES = [
    EvaluationJob.__table__,
    PullRequestEvaluationHead.__table__,
    WebhookDelivery.__table__,
    DispatchIntent.__table__,
    EvaluationAttempt.__table__,
    ModelPacket.__table__,
]
