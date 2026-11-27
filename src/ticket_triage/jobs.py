"""Postgres work queue. Jobs are enqueued in the same transaction as the
state change that requires them, so no work is lost or run twice for a
committed state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ticket_triage.models import Job

TRIAGE = "triage"
CRM_SYNC = "crm_sync"


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    ticket_id: uuid.UUID
    attempts: int
    max_attempts: int


def enqueue(session: Session, kind: str, ticket_id: uuid.UUID, max_attempts: int) -> None:
    session.add(Job(kind=kind, ticket_id=ticket_id, max_attempts=max_attempts))


def claim(session: Session, worker_id: str, stale_after_seconds: int) -> ClaimedJob | None:
    """Lock the next runnable job. A lock older than ``stale_after_seconds``
    belongs to a crashed worker and is taken over."""
    row = session.execute(
        text(
            """
            UPDATE jobs SET locked_at = now(), locked_by = :worker, attempts = attempts + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE done_at IS NULL
                  AND run_after <= now()
                  AND (locked_at IS NULL
                       OR locked_at < now() - make_interval(secs => :stale))
                ORDER BY run_after, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, kind, ticket_id, attempts, max_attempts
            """
        ),
        {"worker": worker_id, "stale": stale_after_seconds},
    ).one_or_none()
    if row is None:
        return None
    return ClaimedJob(row.id, row.kind, row.ticket_id, row.attempts, row.max_attempts)


def complete(session: Session, job_id: int) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.done_at = datetime.now(UTC)
        job.locked_at = None
        job.last_error = None


def fail(session: Session, job: ClaimedJob, error: str, *, retryable: bool) -> bool:
    """Record a failure. Returns True when the job will not be retried."""
    row = session.get(Job, job.id)
    if row is None:
        return True
    row.last_error = error[:4000]
    row.locked_at = None
    row.locked_by = None
    if not retryable or job.attempts >= job.max_attempts:
        row.done_at = datetime.now(UTC)
        row.failed = True
        return True
    row.run_after = datetime.now(UTC) + timedelta(seconds=backoff_seconds(job.attempts))
    return False


def backoff_seconds(attempt: int) -> float:
    return float(min(15 * 2 ** (attempt - 1), 3600))


def pending_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Job).where(Job.done_at.is_(None))) or 0
