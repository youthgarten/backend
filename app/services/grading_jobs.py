from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Attempt, GradingJob
from app.services.attempts import GRACE, transition

LEASE = timedelta(minutes=15)
MAX_ATTEMPTS = 3
RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=30))


def claim(factory, attempt_id: int, now: datetime) -> str | None:
    token = str(uuid4())
    with factory.begin() as session:
        job = session.scalar(
            select(GradingJob).where(GradingJob.attempt_id == attempt_id).with_for_update()
        )
        if (
            job is None
            or job.available_at > now
            or (job.leased_until is not None and job.leased_until > now)
        ):
            return None
        if job.attempt_count >= MAX_ATTEMPTS:
            attempt = session.get(Attempt, attempt_id, with_for_update=True)
            if attempt is not None and attempt.status == "GRADING":
                attempt.status = "GRADING_FAILED"
                attempt.grading_failure = job.last_error or "grading retries exhausted"
            session.delete(job)
            return None
        job.lease_token = token
        job.leased_until = now + LEASE
        job.attempt_count += 1
    return token


def owns_lease(
    session: Session, attempt_id: int, token: str, now: datetime | None = None
) -> GradingJob | None:
    filters = [
        GradingJob.attempt_id == attempt_id,
        GradingJob.lease_token == token,
    ]
    if now is not None:
        filters.append(GradingJob.leased_until > now)
    return session.scalar(select(GradingJob).where(*filters).with_for_update())


def retry_or_fail(
    factory, attempt_id: int, token: str, now: datetime, error: str, *, retry: bool
) -> None:
    with factory.begin() as session:
        job = owns_lease(session, attempt_id, token)
        if job is None:
            return
        attempt = session.get(Attempt, attempt_id, with_for_update=True)
        if retry and job.attempt_count < MAX_ATTEMPTS and attempt.status == "GRADING":
            job.available_at = now + RETRY_DELAYS[job.attempt_count - 1]
            job.lease_token = None
            job.leased_until = None
            job.last_error = error[:100]
            return
        if attempt is not None and attempt.status == "GRADING":
            attempt.status = "GRADING_FAILED"
            attempt.grading_failure = error[:100]
        session.delete(job)


def expire_and_list(factory, now: datetime, limit: int = 20) -> list[int]:
    with factory.begin() as session:
        expired = session.scalars(
            select(Attempt)
            .where(
                Attempt.status == "IN_PROGRESS",
                Attempt.deadline_at < now - GRACE,
            )
            .order_by(Attempt.deadline_at, Attempt.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for attempt in expired:
            transition(session, attempt, "", now, auto=True)
        jobs = session.scalars(
            select(GradingJob.attempt_id)
            .where(
                GradingJob.available_at <= now,
                or_(GradingJob.leased_until.is_(None), GradingJob.leased_until <= now),
            )
            .order_by(GradingJob.available_at, GradingJob.attempt_id)
            .limit(limit)
        ).all()
    return list(jobs)


def recover_grading(factory) -> None:
    """Fail only legacy in-flight attempts that have no durable job."""
    with factory.begin() as session:
        session.execute(
            update(Attempt)
            .where(
                Attempt.status == "GRADING",
                ~select(GradingJob.attempt_id)
                .where(GradingJob.attempt_id == Attempt.id)
                .exists(),
            )
            .values(
                status="GRADING_FAILED",
                grading_failure="missing durable grading job",
            )
        )


def queue_status(factory, now: datetime) -> dict[str, int]:
    with factory() as session:
        pending = session.scalar(select(func.count()).select_from(GradingJob)) or 0
        leased = (
            session.scalar(
                select(func.count())
                .select_from(GradingJob)
                .where(GradingJob.leased_until > now)
            )
            or 0
        )
        stale = (
            session.scalar(
                select(func.count())
                .select_from(GradingJob)
                .where(
                    GradingJob.available_at <= now - timedelta(minutes=5),
                    or_(
                        GradingJob.leased_until.is_(None),
                        GradingJob.leased_until <= now,
                    ),
                )
            )
            or 0
        )
    return {"pending": pending, "leased": leased, "stale": stale}
