"""Idempotent projections, lifecycle synchronization and repository read queries."""

import json
from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import defer, load_only

from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.github_app.service import GitHubAppError
from ai_services.ingestion.persistence.models import RepositoryModel

from .models import EvaluationJob
from .read_models import (
    CheckPublication,
    EvaluationProjection,
    FindingRule,
    PullRequestState,
    PullRequestSyncIntent,
)
from .store import EvaluationStore

LIGHT_JOBS = (
    defer(EvaluationJob.result_json),
    defer(EvaluationJob.context_json),
    defer(EvaluationJob.changes_json),
)


def project(session, job, result, now):
    """Caller owns repository lock and transaction containing the result write."""
    if (
        result.revision.repository_id,
        result.revision.pull_request_number,
        result.revision.revision_key,
    ) != (job.repository_id, job.pr_number, job.revision_key):
        raise ValueError("Projection result revision mismatch")
    row = session.get(EvaluationProjection, job.id)
    if row and row.evaluation_id == result.evaluation_id:
        queue_publication(session, job, now, result.evaluation_id)
        return
    if row is None:
        row = EvaluationProjection(job_id=job.id)
        session.add(row)
    row.evaluation_id, row.outcome, row.execution = (
        result.evaluation_id,
        result.outcome,
        result.execution,
    )
    row.coverage_json = result.coverage.model_dump_json()
    row.confirmed = sum(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings)
    row.review_required = sum(
        f.disposition in {"NEEDS_REVIEW", "ADVISORY"} for f in result.findings
    )
    row.rejected, row.evaluated_at = len(result.rejected_findings), now
    context = json.loads(job.context_json or "{}")
    if context and context.get("context_hash") != result.context_hash:
        raise ValueError("Projection context/result mismatch")
    requirements = {r["requirement_id"]: r for r in context.get("requirements", [])}
    plan = {u.unit_id: u for u in result.plan}
    session.execute(delete(FindingRule).where(FindingRule.job_id == job.id))
    for finding in result.findings:
        unit = plan[finding.unit_id]
        requirement = (
            requirements.get(unit.rule_id, {}) if unit.channel == "ADR" else {}
        )
        # Only a recorded DECISION identity is a decision. Shared ADR, target
        # entity, similar prose or adjacent constraints do not establish a map.
        session.add(
            FindingRule(
                job_id=job.id,
                finding_id=finding.finding_id,
                rule_id=unit.rule_id,
                decision_id=unit.rule_id
                if requirement.get("kind") == "DECISION"
                else None,
                requirement_kind=requirement.get("kind"),
                adr_ids_json=json.dumps(requirement.get("adr_ids", [])),
                classification="CONFIRMED"
                if finding.disposition == "SUPPORTED_CONFLICT"
                else "POTENTIAL"
                if finding.disposition in {"NEEDS_REVIEW", "ADVISORY"}
                else "RESOLVED",
            )
        )
    queue_publication(session, job, now, result.evaluation_id)


def queue_publication(session, job, now, evaluation_id=None):
    from .check_lifecycle import enabled, intent
    if enabled():
        intent(session, job, now, evaluation_id=evaluation_id)
        return
    row = session.get(CheckPublication, job.id)
    if row is None:
        row = CheckPublication(
            job_id=job.id, head_sha=job.head_sha, created_at=now, attempts=0
        )
        session.add(row)
    elif row.evaluation_id == evaluation_id and row.status != "STALE":
        return
    row.evaluation_id, row.next_attempt_at, row.updated_at = evaluation_id, now, now
    if row.status != 'PUBLISHING' or not row.lease_until or row.lease_until <= now:
        row.status = 'PENDING'
    row.error_code = None


def sync_pr(store, repository_id, pr, *, observed_at):
    """Only authoritative GitHub GET responses enter here, never webhook deltas.

    GitHub updated_at plus request-start time fence delayed concurrent responses.
    Equal-second contradictory snapshots fail closed instead of rolling back.
    """
    number = pr["number"]
    if (
        type(number) is not int
        or number <= 0
        or pr.get("state") not in {"open", "closed"}
    ):
        raise ValueError("Invalid authoritative PR metadata")
    updated = datetime.fromisoformat(pr["updated_at"]).timestamp()
    base, head = pr["base"], pr["head"]
    for sha in (base["sha"], head["sha"]):
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid authoritative PR commit")
    state = "merged" if pr.get("merged") else pr["state"]
    with store.sessions.begin() as session:
        EvaluationStore._repository_lock(session, repository_id)
        row = session.get(PullRequestState, (repository_id, number))
        if row:
            if updated < row.github_updated_at or observed_at < row.observed_at:
                return False
            if updated == row.github_updated_at and (
                row.head_sha,
                row.base_sha,
                row.state,
            ) != (head["sha"], base["sha"], state):
                row.freshness = "UNKNOWN"
                return False
        else:
            row = PullRequestState(repository_id=repository_id, pr_number=number)
            session.add(row)
        row.title, row.author = pr["title"], (pr.get("user") or {}).get("login")
        row.base_branch, row.head_branch = base["ref"], head["ref"]
        row.base_sha, row.head_sha, row.state = base["sha"], head["sha"], state
        row.github_updated_at, row.observed_at, row.freshness = (
            updated,
            observed_at,
            "OBSERVED",
        )
        return True


async def synchronize(store, github, repository_id, number):
    repo = github.store.get_repository(repository_id)
    started = store.clock()
    try:
        await github.indexing_credential(repo.id, repo.installation_id)
        credential = await github.installation_token(
            repo.installation_id,
            (repo.github_repository_id,),
            permissions={
                "contents": "read",
                "metadata": "read",
                "pull_requests": "read",
            },
        )
        pr = await github.request(
            "GET", f"/repos/{repo.full_name}/pulls/{number}", credential.token
        )
        if (pr.get("number") != number or str(pr["base"]["repo"]["id"]) != repo.github_repository_id):
            raise ValueError("PR repository identity mismatch")
        applied = sync_pr(store, repository_id, pr, observed_at=started)
    except (GitHubAppError, KeyError, TypeError, ValueError) as error:
        with store.sessions.begin() as session:
            EvaluationStore._repository_lock(session, repository_id)
            row = session.get(PullRequestState, (repository_id, number))
            if row and row.observed_at <= started:
                row.freshness = "UNKNOWN"
            intent = session.get(PullRequestSyncIntent, (repository_id, number))
            if intent:
                intent.attempts += 1
                intent.next_attempt_at = store.clock() + min(
                    300, 2 ** min(intent.attempts, 8)
                )
                intent.error_code = "GITHUB_METADATA_UNAVAILABLE"
        if isinstance(error, GitHubAppError):
            raise
        raise GitHubAppError(502, "Invalid GitHub PR metadata; retry synchronization") from None
    with store.sessions.begin() as session:
        EvaluationStore._repository_lock(session, repository_id)
        intent = session.get(PullRequestSyncIntent, (repository_id, number))
        if intent and intent.requested_at <= started:
            if applied:
                session.delete(intent)
            else:
                intent.next_attempt_at = store.clock() + 60
    return pr


def request_sync(store, repository_id, number):
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid PR number")
    with store.sessions.begin() as session:
        EvaluationStore._repository_lock(session, repository_id)
        intent = session.get(PullRequestSyncIntent, (repository_id, number))
        if intent is None:
            intent = PullRequestSyncIntent(
                repository_id=repository_id, pr_number=number
            )
            session.add(intent)
        intent.requested_at = intent.next_attempt_at = store.clock()


async def synchronize_due(store, github):
    with store.sessions() as session:
        pending = list(
            session.scalars(
                select(PullRequestSyncIntent)
                .where(PullRequestSyncIntent.next_attempt_at <= store.clock())
                .limit(50)
            )
        )
    for intent in pending:
        try:
            await synchronize(store, github, intent.repository_id, intent.pr_number)
        except GitHubAppError:
            pass  # durable metadata intent survives; no evaluation is scheduled


def backfill(store):
    """Explicit migration helper. Never queues evaluation or changes job JSON."""
    with store.sessions() as session:
        ids = list(
            session.scalars(
                select(EvaluationJob.id).where(EvaluationJob.result_json.is_not(None))
            )
        )
    for job_id in ids:
        with store.sessions.begin() as session:
            job = session.get(EvaluationJob, job_id)
            EvaluationStore._repository_lock(session, job.repository_id)
            project(
                session,
                job,
                EvaluationResult.model_validate_json(job.result_json),
                job.updated_at,
            )


def publication_data(row):
    if row is None:
        return {"status": "NOT_QUEUED", "attempts": 0}
    return {
        k: getattr(row, k)
        for k in (
            "status",
            "head_sha",
            "check_run_id",
            "conclusion",
            "attempts",
            "updated_at",
            "published_at",
            "error_code",
        )
    }


def evaluation_data(job, projection=None, publication=None):
    if job is None:
        return None
    return {
        "job_id": job.id,
        "revision_key": job.revision_key,
        "base_sha": job.base_sha,
        "head_sha": job.head_sha,
        "status": job.status,
        "stage": job.stage,
        "created_at": job.created_at,
        "evaluated_at": projection.evaluated_at if projection else None,
        "outcome": projection.outcome if projection else None,
        "execution": projection.execution if projection else None,
        "coverage": json.loads(projection.coverage_json) if projection else None,
        "confirmed_conflicts": projection.confirmed if projection else 0,
        "review_required_findings": projection.review_required if projection else 0,
        "rejected_findings": projection.rejected if projection else 0,
        "publication": publication_data(publication),
    }


class RepositoryReadModel:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _latest(session, repository_id, number, state=None):
        query = (
            select(EvaluationJob)
            .options(*LIGHT_JOBS)
            .where(
                EvaluationJob.repository_id == repository_id,
                EvaluationJob.pr_number == number,
            )
        )
        if state:
            query = query.where(
                EvaluationJob.head_sha == state.head_sha,
                EvaluationJob.base_sha == state.base_sha,
            )
        return session.scalar(
            query.order_by(EvaluationJob.created_at.desc(), EvaluationJob.id).limit(1)
        )

    def list_prs(self, repository_id, limit=50, offset=0):
        # One row per PR, including historical jobs predating lifecycle sync.
        with self.store.sessions() as session:
            numbers = (
                select(EvaluationJob.pr_number.label("number"))
                .where(EvaluationJob.repository_id == repository_id)
                .union(
                    select(PullRequestState.pr_number).where(
                        PullRequestState.repository_id == repository_id
                    )
                )
                .subquery()
            )
            total = session.scalar(select(func.count()).select_from(numbers))
            selected = list(
                session.scalars(
                    select(numbers.c.number)
                    .order_by(numbers.c.number.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            return {
                "items": [self._pr(session, repository_id, n) for n in selected],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def _pr(self, session, repository_id, number):
        state = session.get(PullRequestState, (repository_id, number))
        latest = self._latest(session, repository_id, number, state)
        fallback = latest or self._latest(session, repository_id, number)
        if not state and not fallback:
            return None
        event = json.loads(fallback.event_json) if fallback else {}
        repository = session.get(RepositoryModel, repository_id)
        data = {
            "repository_id": repository_id,
            "repository_full_name": repository.full_name,
            "github_url": f"https://github.com/{repository.full_name}/pull/{number}",
            "pr_number": number,
            **{
                k: getattr(state, k) if state else event.get(k)
                for k in (
                    "title",
                    "author",
                    "base_branch",
                    "head_branch",
                    "base_sha",
                    "head_sha",
                )
            },
            "state": state.state if state else "unknown",
            "observed_at": state.observed_at if state else None,
            "github_updated_at": state.github_updated_at if state else None,
            "metadata_freshness": state.freshness if state else "UNKNOWN",
            "freshness": "CURRENT_OBSERVED"
            if state and state.freshness == "OBSERVED" and latest
            else "STALE"
            if state and fallback
            else "UNKNOWN",
            "evaluation": evaluation_data(
                latest,
                session.get(EvaluationProjection, latest.id),
                session.get(CheckPublication, latest.id),
            )
            if latest
            else None,
        }
        return data

    def detail(self, repository_id, number, limit=20, offset=0):
        with self.store.sessions() as session:
            pr = self._pr(session, repository_id, number)
            if pr is None:
                return None
            jobs = list(
                session.scalars(
                    select(EvaluationJob)
                    .options(*LIGHT_JOBS)
                    .where(
                        EvaluationJob.repository_id == repository_id,
                        EvaluationJob.pr_number == number,
                    )
                    .order_by(EvaluationJob.created_at.desc(), EvaluationJob.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            latest_id = (pr["evaluation"] or {}).get("job_id")
            latest = session.get(EvaluationJob, latest_id) if latest_id else None
            return {
                "pull_request": pr,
                "result": EvaluationResult.model_validate_json(latest.result_json)
                if latest and latest.result_json
                else None,
                "deferred": json.loads(latest.deferred_json) if latest else [],
                "governing_rules": [
                    {
                        "finding_id": f.finding_id,
                        "rule_id": f.rule_id,
                        "decision_id": f.decision_id,
                        "requirement_kind": f.requirement_kind,
                        "adr_ids": json.loads(f.adr_ids_json),
                        "classification": f.classification,
                    }
                    for f in session.scalars(
                        select(FindingRule).where(FindingRule.job_id == latest_id)
                    )
                ],
                "history": [
                    evaluation_data(
                        j,
                        session.get(EvaluationProjection, j.id),
                        session.get(CheckPublication, j.id),
                    )
                    for j in jobs
                ],
                "history_total": session.scalar(
                    select(func.count())
                    .select_from(EvaluationJob)
                    .where(
                        EvaluationJob.repository_id == repository_id,
                        EvaluationJob.pr_number == number,
                    )
                ),
                "limit": limit,
                "offset": offset,
            }

    def overview(self, repository_id):
        with self.store.sessions() as session:
            # Current-head matches on BOTH base and head. Closed and unknown PRs
            # never contribute. No network or full evidence JSON in aggregation.
            current = (
                select(EvaluationJob.id)
                .join(
                    PullRequestState,
                    and_(
                        PullRequestState.repository_id == EvaluationJob.repository_id,
                        PullRequestState.pr_number == EvaluationJob.pr_number,
                        PullRequestState.base_sha == EvaluationJob.base_sha,
                        PullRequestState.head_sha == EvaluationJob.head_sha,
                    ),
                )
                .where(
                    EvaluationJob.repository_id == repository_id,
                    PullRequestState.state == "open",
                    PullRequestState.freshness == "OBSERVED",
                )
            )
            rules = list(
                session.scalars(
                    select(FindingRule).where(
                        FindingRule.job_id.in_(current),
                        FindingRule.classification.in_(["CONFIRMED", "POTENTIAL"]),
                    )
                )
            )
            confirmed = {
                f.decision_id
                for f in rules
                if f.decision_id and f.classification == "CONFIRMED"
            }
            potential = {
                f.decision_id
                for f in rules
                if f.decision_id and f.classification == "POTENTIAL"
            } - confirmed
            unmapped = {
                f.rule_id for f in rules if not f.decision_id and f.requirement_kind
            }
            jobs_by_id = {
                j.id: j
                for j in session.scalars(
                    select(EvaluationJob)
                    .options(load_only(EvaluationJob.id, EvaluationJob.pr_number))
                    .where(EvaluationJob.id.in_(current))
                )
            }
            risks = {}
            for f in rules:
                identity = f.decision_id or f.rule_id
                key = ("DECISION" if f.decision_id else "REQUIREMENT", identity)
                item = risks.setdefault(
                    key,
                    {
                        "identity": identity,
                        "kind": key[0],
                        "classification": "POTENTIAL",
                        "pr_numbers": set(),
                        "finding_ids": set(),
                        "adr_ids": set(),
                    },
                )
                if f.classification == "CONFIRMED":
                    item["classification"] = "CONFIRMED"
                item["pr_numbers"].add(jobs_by_id[f.job_id].pr_number)
                item["finding_ids"].add(f.finding_id)
                item["adr_ids"].update(json.loads(f.adr_ids_json))
            projections = list(
                session.scalars(
                    select(EvaluationProjection).where(
                        EvaluationProjection.job_id.in_(current)
                    )
                )
            )
            recent = list(
                session.execute(
                    select(EvaluationJob, EvaluationProjection, CheckPublication, PullRequestState)
                    .options(*LIGHT_JOBS)
                    .join(EvaluationProjection)
                    .outerjoin(CheckPublication, CheckPublication.job_id == EvaluationJob.id)
                    .outerjoin(PullRequestState, and_(
                        PullRequestState.repository_id == EvaluationJob.repository_id,
                        PullRequestState.pr_number == EvaluationJob.pr_number,
                    ))
                    .where(EvaluationJob.repository_id == repository_id)
                    .order_by(
                        EvaluationProjection.evaluated_at.desc(), EvaluationJob.id
                    )
                    .limit(10)
                )
            )
            return {
                "prs_analyzed": session.scalar(
                    select(func.count(func.distinct(EvaluationJob.pr_number))).where(
                        EvaluationJob.repository_id == repository_id,
                        EvaluationJob.result_json.is_not(None),
                    )
                ),
                "decisions_at_risk": len(confirmed | potential),
                "confirmed_decisions": len(confirmed),
                "potential_decisions": len(potential),
                "unmapped_requirements": len(unmapped),
                "attention_total": len(risks),
                "attention": [
                    {k: sorted(v) if isinstance(v, set) else v for k, v in item.items()}
                    for _, item in sorted(risks.items())[:50]
                ],
                "architectural_warnings": sum(
                    p.confirmed + p.review_required + p.rejected for p in projections
                ),
                "confirmed_warnings": sum(p.confirmed for p in projections),
                "potential_warnings": sum(
                    p.review_required + p.rejected for p in projections
                ),
                "uncertain_units": sum(
                    json.loads(p.coverage_json)["uncertain"] for p in projections
                ),
                "recent": [
                    {
                        "pr_number": j.pr_number,
                        **evaluation_data(j, p, publication),
                        "state": s.state if s else "unknown",
                        "freshness": "CURRENT_OBSERVED"
                        if s
                        and s.freshness == "OBSERVED"
                        and (s.head_sha, s.base_sha) == (j.head_sha, j.base_sha)
                        else "STALE"
                        if s
                        else "UNKNOWN",
                        "title": s.title if s else json.loads(j.event_json or '{}').get('title'),
                        "observed_at": s.observed_at if s else None,
                    }
                    for j, p, publication, s in recent
                ],
            }
