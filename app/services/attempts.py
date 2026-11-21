from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import AnswerRevision, Attempt, GradingJob, Task, TaskMaterial, User

GRACE = timedelta(minutes=1)
RESULT_STATES = ("GRADING", "GRADED", "GRADING_FAILED")


def iso(moment: datetime | None) -> str | None:
    return (
        moment.replace(tzinfo=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        if moment
        else None
    )


def lock_owner(session: Session, user_id: int) -> None:
    session.execute(select(User.id).where(User.id == user_id).with_for_update()).one()


def own_attempt(
    session: Session, user_id: int, attempt_id: int, *, lock: bool = False
) -> Attempt:
    query = select(Attempt).where(Attempt.id == attempt_id, Attempt.user_id == user_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    attempt = session.scalar(query)
    if attempt is None:
        raise ApiError("ATTEMPT_NOT_FOUND")
    return attempt


def active_attempt(session: Session, user_id: int) -> Attempt | None:
    return session.scalar(
        select(Attempt)
        .where(Attempt.in_progress_uid == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def require_writable(attempt: Attempt, now: datetime) -> None:
    if attempt.status != "IN_PROGRESS":
        raise ApiError("ATTEMPT_NOT_IN_PROGRESS")
    if now > attempt.deadline_at:
        raise ApiError("ATTEMPT_TIME_OVER")


def last_revision(
    session: Session, attempt_id: int, *, lock: bool = False
) -> AnswerRevision | None:
    query = select(AnswerRevision).where(AnswerRevision.attempt_id == attempt_id)
    query = query.order_by(AnswerRevision.id.desc()).limit(1)
    return session.scalar(query.with_for_update() if lock else query)


def make_task_snapshot(session: Session, task: Task) -> dict:
    materials = session.scalars(
        select(TaskMaterial)
        .where(TaskMaterial.task_id == task.id)
        .order_by(TaskMaterial.sort_order, TaskMaterial.id)
    )
    return {
        "id": task.id,
        "title": task.title,
        "situation": task.situation,
        "instruction": task.instruction,
        "materials": [
            {"id": item.id, "title": item.title, "content": item.content}
            for item in materials
        ],
    }


def task_snapshot(session: Session, attempt: Attempt) -> dict:
    if attempt.task_snapshot is not None:
        return attempt.task_snapshot
    task = session.get(Task, attempt.task_id)
    if task is None:
        raise RuntimeError(f"Attempt {attempt.id} refers to a missing task.")
    return make_task_snapshot(session, task)


def queue_grading(session: Session, attempt_id: int, now: datetime) -> None:
    statement = insert(GradingJob).values(
        attempt_id=attempt_id,
        available_at=now,
        lease_token=None,
        leased_until=None,
        attempt_count=0,
        last_error=None,
    )
    session.execute(
        statement.on_duplicate_key_update(
            available_at=now,
            lease_token=None,
            leased_until=None,
            attempt_count=0,
            last_error=None,
        )
    )


def transition(
    session: Session,
    attempt: Attempt,
    answer: str,
    now: datetime,
    *,
    auto: bool = False,
) -> bool:
    deadline = (
        Attempt.deadline_at < now - GRACE
        if auto
        else Attempt.deadline_at >= now - GRACE
    )
    final_answer = answer
    if now > attempt.deadline_at:
        revision = last_revision(session, attempt.id, lock=True)
        final_answer = revision.content if revision else ""
    result = session.execute(
        update(Attempt)
        .where(
            Attempt.id == attempt.id,
            Attempt.user_id == attempt.user_id,
            Attempt.status == "IN_PROGRESS",
            deadline,
        )
        .values(status="GRADING", answer=final_answer, submitted_at=now)
    )
    if result.rowcount == 1:
        queue_grading(session, attempt.id, now)
        return True
    return False


def expire_active(
    session: Session, user_id: int, now: datetime
) -> tuple[Attempt | None, int | None]:
    active = active_attempt(session, user_id)
    if active is None or now <= active.deadline_at + GRACE:
        return active, None
    if transition(session, active, "", now, auto=True):
        return None, active.id
    return None, None
