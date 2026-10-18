"""Phase A rollout switch and durable publication intent (no provider I/O)."""
import os

from sqlalchemy import select

from .read_models import CheckPublication

LIFECYCLE_COLUMNS = {'desired_state', 'desired_version', 'published_version'}


def enabled():
    # Off is the compatibility/rollback mode, including before additive DDL.
    return os.getenv('GITHUB_CHECK_LIFECYCLE_ENABLED', 'false').lower() == 'true'


def queue_max_age():
    age = int(os.getenv('GITHUB_CHECK_QUEUE_MAX_AGE_SECONDS', '1800'))
    if not 1 <= age <= 86400:
        raise ValueError('GITHUB_CHECK_QUEUE_MAX_AGE_SECONDS must be between 1 and 86400')
    return age


def desired_state(job):
    if job.status == 'QUEUED':
        return 'queued'
    if job.status in {'RUNNING', 'RETRY'}:
        return 'in_progress'
    return 'completed'


def intent(session, job, now, *, evaluation_id=None):
    """Caller owns the job transaction. Never steal an active publisher lease."""
    row = session.scalar(select(CheckPublication).where(
        CheckPublication.job_id == job.id).with_for_update())
    if row is None:
        row = CheckPublication(job_id=job.id, head_sha=job.head_sha,
                               created_at=now, attempts=0, status='PENDING',
                               next_attempt_at=now, updated_at=now)
        session.add(row)
        # Server defaults permit the compatibility image to insert on the old
        # schema. In enabled mode the migration is mandatory.
        row.desired_state, row.desired_version, row.published_version = 'queued', 0, 0
    target = desired_state(job)
    rank = {'queued': 0, 'in_progress': 1, 'completed': 2}
    # A completed Check is not reset by redelivery or existing continuation.
    # Continuation can still publish a new final evaluation identity later.
    if row.desired_version and rank[target] < rank[row.desired_state]:
        return row
    if (not row.desired_version or target != row.desired_state or
            (evaluation_id is not None and row.evaluation_id != evaluation_id)):
        row.desired_state = target
        row.desired_version += 1
        if evaluation_id is not None:
            row.evaluation_id = evaluation_id
        row.updated_at, row.next_attempt_at = now, now
        row.error_code = None
        if row.status != 'PUBLISHING' or not row.lease_until or row.lease_until <= now:
            # Known identity/permission blocks need explicit operator retry.
            if row.status != 'BLOCKED':
                row.status = 'PENDING'
    return row
