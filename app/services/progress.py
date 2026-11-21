from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.models import Lecture, LectureProgress

UNREPORTED = datetime(1970, 1, 1)


def merge_watched_ranges(existing, incoming, duration_sec):
    ranges = []
    for start, end in sorted((*existing, *incoming)):
        start, end = max(start, 0), min(end, duration_sec)
        if end <= start:
            continue
        if ranges and start <= ranges[-1][1]:
            ranges[-1][1] = max(end, ranges[-1][1])
        else:
            ranges.append([start, end])
    return ranges


def budget_ranges(incoming, duration, remaining):
    filtered = []
    for start, end in incoming:
        start, end = max(start, 0), min(end, duration, max(start, 0) + remaining)
        if end > start:
            filtered.append([start, end])
            remaining -= end - start
        if remaining == 0:
            break
    return filtered


def progress_info(lecture: Lecture, progress: LectureProgress | None) -> dict:
    if progress is None:
        return {"progress_rate": 0.0, "completed": False}
    completed = progress.completed_at is not None
    rate = round(progress.watched_sec / lecture.duration_sec, 2)
    # Avoid displaying the required 80% while the exact eligibility check is false.
    return {
        "progress_rate": rate if completed else min(rate, 0.79),
        "completed": completed,
    }


def record_progress(
    session: Session,
    user_id: int,
    lecture: Lecture,
    ranges,
    position: int,
    now: datetime,
) -> dict:
    start = insert(LectureProgress).values(
        user_id=user_id,
        lecture_id=lecture.id,
        watched_ranges=[],
        watched_sec=0,
        last_position_sec=0,
        updated_at=UNREPORTED,
    )
    session.execute(
        start.on_duplicate_key_update(updated_at=LectureProgress.updated_at)
    )
    progress = session.scalar(
        select(LectureProgress)
        .where(
            LectureProgress.user_id == user_id,
            LectureProgress.lecture_id == lecture.id,
        )
        .with_for_update()
    )
    duration = lecture.duration_sec
    first_report = progress.updated_at == UNREPORTED
    watermark = max(
        progress.updated_at, progress.watch_budget_used_until or progress.updated_at
    )
    budget = 15 if first_report else max(0, int((now - watermark).total_seconds()) + 5)
    accepted = budget_ranges(ranges, duration, budget)
    credited = sum(end - start for start, end in accepted)
    # Time already credited against the five-second tolerance cannot be credited again.
    progress.watch_budget_used_until = (
        now if first_report else watermark + timedelta(seconds=credited)
    )
    progress.watched_ranges = merge_watched_ranges(
        progress.watched_ranges, accepted, duration
    )
    progress.watched_sec = sum(end - start for start, end in progress.watched_ranges)
    progress.last_position_sec = max(0, min(position, duration))
    progress.updated_at = max(now, progress.updated_at)
    just_completed = (
        progress.completed_at is None and progress.watched_sec * 5 >= duration * 4
    )
    if just_completed:
        progress.completed_at = now
    return {**progress_info(lecture, progress), "just_completed": just_completed}
