"""Durable GitHub Checks delivery. This module has no model dependency."""

import uuid

from sqlalchemy import and_, or_, select, update

from ai_services.decision_guard.evaluation.models import EvaluationResult
from ai_services.github_app.service import GitHubAppError

from .check_presentation import details_url, render_review
from .projections import synchronize
from .read_models import CheckPublication, PullRequestState

CHECK_NAME = "DecisionGuard / Architecture"


def conclusion(result):
    if result is None or result.outcome == "NOT_EVALUATED":
        return "action_required"
    if any(f.disposition == "SUPPORTED_CONFLICT" for f in result.findings):
        return "failure"
    if (
        result.outcome != "NO_CONFLICTS_FOUND"
        or result.execution != "COMPLETED"
        or result.coverage.scope != "COMPLETE_FOR_RETAINED_SCOPE"
        or result.coverage.uncertain
        or result.coverage.omitted
        or result.coverage.errors
        or result.limitations
        or result.rejected_findings
        or any(f.disposition in {"NEEDS_REVIEW", "ADVISORY"} for f in result.findings)
    ):
        return "neutral"
    return "success"


def check_payload(job, result, full_name, *, details_base=None, rules=()):
    # rules is retained for caller compatibility. Presentation reads only the
    # canonical result; detailed normalized associations remain in PR Details.
    payload = {
        "name": CHECK_NAME,
        "head_sha": job.head_sha,
        "external_id": f"decisionguard:{job.id}",
        "status": "completed",
        "conclusion": conclusion(result),
        "output": render_review(result, full_name),
    }
    if target := details_url(details_base, job.repository_id, job.pr_number):
        payload["details_url"] = target
    return payload


class CheckPublisher:
    def __init__(
        self, store, github, *, enabled=False, details_base=None, required_check_id=None
    ):
        self.store, self.github, self.enabled, self.details_base = (
            store,
            github,
            enabled,
            details_base,
        )
        self.required_check_id = (
            str(required_check_id) if required_check_id is not None else None
        )

    def due(self):
        now = self.store.clock()
        with self.store.sessions() as session:
            return list(
                session.scalars(
                    select(CheckPublication.job_id)
                    .where(
                        or_(
                            and_(
                                CheckPublication.status.in_(
                                    ["PENDING", "RETRY", "CREATE_UNKNOWN"]
                                ),
                                CheckPublication.next_attempt_at <= now,
                            ),
                            and_(
                                CheckPublication.status == "PUBLISHING",
                                CheckPublication.lease_until <= now,
                            ),
                        )
                    )
                    .limit(50)
                )
            )

    def claim(self, job_id):
        now, token = self.store.clock(), uuid.uuid4().hex
        with self.store.sessions.begin() as session:
            changed = session.execute(
                update(CheckPublication)
                .where(
                    CheckPublication.job_id == job_id,
                    or_(
                        and_(
                            CheckPublication.status.in_(
                                ["PENDING", "RETRY", "CREATE_UNKNOWN"]
                            ),
                            CheckPublication.next_attempt_at <= now,
                        ),
                        and_(
                            CheckPublication.status == "PUBLISHING",
                            CheckPublication.lease_until <= now,
                        ),
                    ),
                )
                .values(
                    status="PUBLISHING",
                    lease_token=token,
                    lease_until=now + 180,
                    attempts=CheckPublication.attempts + 1,
                    updated_at=now,
                )
            )
            return token if changed.rowcount == 1 else None

    def save(self, job_id, token, **values):
        with self.store.sessions.begin() as session:
            changed = session.execute(
                update(CheckPublication)
                .where(
                    CheckPublication.job_id == job_id,
                    CheckPublication.lease_token == token,
                    CheckPublication.status == "PUBLISHING",
                    CheckPublication.lease_until > self.store.clock(),
                )
                .values(updated_at=self.store.clock(), **values)
            )
            if changed.rowcount != 1:
                raise RuntimeError("Publication ownership lost")

    def retry(self, job_id):
        """Operator retry independent of evaluation; unknown POSTs stay reconcile-only."""
        with self.store.sessions.begin() as session:
            row = session.get(CheckPublication, job_id)
            if row is None or row.status == "PUBLISHING":
                raise ValueError("Publication is missing or currently owned")
            row.status, row.next_attempt_at = "RETRY", self.store.clock()

    async def reconcile(self):
        if self.enabled:
            for job_id in self.due():
                await self.publish(job_id)

    async def publish(self, job_id):
        if not self.enabled:
            return
        token = self.claim(job_id)
        if token is None:
            return
        try:
            await self._publish(job_id, token)
        except Exception as error:  # noqa: BLE001 -- publication never alters evaluation status
            with self.store.sessions() as session:
                row = session.get(CheckPublication, job_id)
            code = (
                f"GITHUB_{error.status_code}"
                if isinstance(error, GitHubAppError)
                else type(error).__name__
            )
            status = (
                "BLOCKED"
                if isinstance(error, ValueError)
                or (
                    isinstance(error, GitHubAppError)
                    and error.status_code in {401, 403, 404}
                )
                else "CREATE_UNKNOWN"
                if row.create_started_at and not row.check_run_id
                else "RETRY"
            )
            # Only safe error codes are retained, never source/credential strings.
            try:
                self.save(
                    job_id,
                    token,
                    status=status,
                    error_code=code,
                    next_attempt_at=self.store.clock()
                    + min(3600, 2 ** min(row.attempts, 11)),
                    lease_token=None,
                    lease_until=None,
                )
            except RuntimeError:
                pass

    async def _publish(self, job_id, token):
        job = self.store.get(job_id)
        if job.status in {"RUNNING", "QUEUED", "RETRY"}:
            self.save(
                job_id,
                token,
                status="RETRY",
                next_attempt_at=self.store.clock() + 30,
                lease_token=None,
                lease_until=None,
            )
            return
        with self.store.sessions() as session:
            row = session.get(CheckPublication, job_id)
        result = (
            EvaluationResult.model_validate_json(job.result_json)
            if job.result_json
            else None
        )
        if result and row.evaluation_id != result.evaluation_id:
            raise ValueError("Publication result changed")
        pr = await synchronize(
            self.store, self.github, job.repository_id, job.pr_number
        )
        with self.store.sessions() as session:
            state = session.get(PullRequestState, (job.repository_id, job.pr_number))
        if (
            pr["state"] != "open"
            or state.freshness != "OBSERVED"
            or (state.base_sha, state.head_sha) != (job.base_sha, job.head_sha)
        ):
            self.save(
                job_id,
                token,
                status="STALE",
                error_code="PR_REVISION_NOT_CURRENT",
                lease_token=None,
                lease_until=None,
            )
            return
        repo = self.github.store.get_repository(job.repository_id)
        credential = await self.github.installation_token(
            repo.installation_id,
            (repo.github_repository_id,),
            permissions={
                "checks": "write",
                "metadata": "read",
                "pull_requests": "read",
            },
        )
        prefix = f"/repos/{repo.full_name}"
        check_id = row.check_run_id
        if self.required_check_id is not None and check_id != self.required_check_id:
            raise ValueError("Existing Check identity required; creation is prohibited")
        if not check_id:
            # Find the response after a crash between GitHub commit and our DB commit.
            runs = await self.github.pages(
                f"{prefix}/commits/{job.head_sha}/check-runs",
                credential.token,
                field="check_runs",
                params={"check_name": CHECK_NAME, "filter": "all"},
            )
            matches = [
                r
                for r in runs
                if r.get("external_id") == f"decisionguard:{job.id}"
                and r.get("head_sha") == job.head_sha
                and str((r.get("app") or {}).get("id"))
                == str(self.github.settings.app_id)
            ]
            if len(matches) > 1:
                self.save(
                    job_id,
                    token,
                    status="BLOCKED",
                    error_code="DUPLICATE_REMOTE_CHECKS",
                    lease_token=None,
                    lease_until=None,
                )
                return
            if matches:
                check_id = str(matches[0]["id"])
                self.save(job_id, token, check_run_id=check_id)
            elif row.create_started_at:
                # GitHub has no idempotency key for POST. A timed-out POST may
                # still commit later; absence from a GET is not proof of failure.
                self.save(
                    job_id,
                    token,
                    status="CREATE_UNKNOWN",
                    error_code="CREATE_RESPONSE_UNKNOWN",
                    next_attempt_at=self.store.clock() + 60,
                    lease_token=None,
                    lease_until=None,
                )
                return
        from .read_models import FindingRule

        with self.store.sessions() as session:
            rules = list(
                session.scalars(select(FindingRule).where(FindingRule.job_id == job_id))
            )
        payload = check_payload(
            job, result, repo.full_name, details_base=self.details_base, rules=rules
        )
        if check_id:
            remote = await self.github.request(
                "GET", f"{prefix}/check-runs/{check_id}", credential.token
            )
            validate_remote_check(
                remote, job, repo.full_name, check_id, self.github.settings.app_id
            )
        if "details_url" in payload:
            import httpx

            try:
                accessible = await self.github.client.get(
                    payload["details_url"], timeout=5, follow_redirects=False
                )
                if not 200 <= accessible.status_code < 300:
                    payload.pop("details_url")
            except httpx.HTTPError:
                payload.pop("details_url")
        # Refresh immediately before mutation; old results only ever address
        # their own immutable head SHA and Check ID, never another revision's run.
        pr = await synchronize(
            self.store, self.github, job.repository_id, job.pr_number
        )
        if pr["state"] != "open" or (pr["base"]["sha"], pr["head"]["sha"]) != (
            job.base_sha,
            job.head_sha,
        ):
            self.save(
                job_id,
                token,
                status="STALE",
                error_code="PR_REVISION_NOT_CURRENT",
                lease_token=None,
                lease_until=None,
            )
            return
        if check_id:
            self.save(job_id, token)  # fence immediately before idempotent PATCH
            payload.pop("head_sha")
            response = await self.github.request(
                "PATCH",
                f"{prefix}/check-runs/{check_id}",
                credential.token,
                json=payload,
            )
        else:
            self.save(job_id, token, create_started_at=self.store.clock())
            try:
                response = await self.github.request(
                    "POST", f"{prefix}/check-runs", credential.token, json=payload
                )
            except GitHubAppError as error:
                if error.status_code in {401, 403, 404}:
                    self.save(job_id, token, create_started_at=None)
                raise
        if (
            response.get("head_sha") != job.head_sha
            or response.get("external_id") != f"decisionguard:{job.id}"
            or not isinstance(response.get("id"), int)
            or (check_id and str(response["id"]) != check_id)
        ):
            raise ValueError("GitHub returned mismatched Check identity")
        self.save(
            job_id,
            token,
            status="PUBLISHED",
            check_run_id=str(response["id"]),
            conclusion=payload["conclusion"],
            published_at=self.store.clock(),
            error_code=None,
            lease_token=None,
            lease_until=None,
        )


def validate_remote_check(remote, job, full_name, check_id, app_id):
    """Check immutable identity and App ownership before an existing-run PATCH."""
    if (
        str(remote.get("id")) != str(check_id)
        or remote.get("head_sha") != job.head_sha
        or remote.get("name") != CHECK_NAME
        or remote.get("external_id") != f"decisionguard:{job.id}"
        or str((remote.get("app") or {}).get("id")) != str(app_id)
        or remote.get("url")
        != f"https://api.github.com/repos/{full_name}/check-runs/{check_id}"
    ):
        raise ValueError(
            "Remote Check repository, App, head or publication identity mismatch"
        )
