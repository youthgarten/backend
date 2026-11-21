from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUser, Database
from app.api.schemas import LectureDashboard, LectureDetail, ProgressSaved
from app.errors import ApiError
from app.models import Lecture, LectureProgress, Task
from app.models.catalog import lecture_is_listed
from app.services.progress import progress_info, record_progress

router = APIRouter(prefix="/lectures", tags=["lectures"])


class ProgressReport(BaseModel):
    watched_ranges: Annotated[list[tuple[StrictInt, StrictInt]], Field(max_length=20)]
    position_sec: StrictInt


def own_lecture(
    session: Session, job_id: int, lecture_id: int, *, listed_only: bool = False
) -> Lecture:
    query = select(Lecture).where(Lecture.id == lecture_id, Lecture.job_id == job_id)
    if listed_only:
        query = query.where(lecture_is_listed(Lecture.id))
    lecture = session.scalar(query)
    if lecture is None:
        raise ApiError("LECTURE_NOT_FOUND")
    return lecture


def lecture_summary(lecture: Lecture, progress: LectureProgress | None) -> dict:
    return {
        "id": lecture.id,
        "title": lecture.title,
        "duration_sec": lecture.duration_sec,
        **progress_info(lecture, progress),
    }


@router.get("", response_model=LectureDashboard)
def lectures(user: CurrentUser, session: Database):
    entries = session.execute(
        select(Lecture, LectureProgress)
        .outerjoin(
            LectureProgress,
            and_(
                LectureProgress.lecture_id == Lecture.id,
                LectureProgress.user_id == user.id,
            ),
        )
        .where(Lecture.job_id == user.job_id, lecture_is_listed(Lecture.id))
        .order_by(Lecture.sort_order, Lecture.id)
    )
    rows = [lecture_summary(lecture, progress) for lecture, progress in entries]
    return {
        "lectures": rows,
        "total_count": len(rows),
        "completed_count": sum(row["completed"] for row in rows),
    }


@router.get("/{lecture_id}", response_model=LectureDetail)
def detail(lecture_id: int, user: CurrentUser, session: Database):
    lecture = own_lecture(session, user.job_id, lecture_id)
    progress = session.get(LectureProgress, (user.id, lecture.id))
    task_id = session.scalar(
        select(Task.id).where(Task.lecture_id == lecture.id, Task.job_id == user.job_id)
    )
    return {
        **lecture_summary(lecture, progress),
        "youtube_video_id": lecture.youtube_video_id,
        "last_position_sec": progress.last_position_sec if progress else 0,
        "task_id": task_id,
    }


@router.post("/{lecture_id}/progress", response_model=ProgressSaved)
def progress(
    lecture_id: int,
    body: ProgressReport,
    user: CurrentUser,
    session: Database,
    request: Request,
):
    lecture = own_lecture(session, user.job_id, lecture_id, listed_only=True)
    data = record_progress(
        session,
        user.id,
        lecture,
        body.watched_ranges,
        body.position_sec,
        request.app.state.clock(),
    )
    session.commit()
    return data
