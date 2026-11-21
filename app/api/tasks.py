from fastapi import APIRouter
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUser, Database
from app.api.schemas import TaskDetail, TaskListing
from app.errors import ApiError
from app.models import Attempt, Lecture, LectureProgress, Task, TaskMaterial, User
from app.models.catalog import lecture_is_listed
from app.services.rubrics import public_criteria, public_requirements

router = APIRouter(prefix="/tasks", tags=["tasks"])


def own_task(session: Session, user: User, task_id: int) -> Task:
    task = session.scalar(
        select(Task)
        .join(Lecture, Task.lecture_id == Lecture.id)
        .where(
            Task.id == task_id,
            Task.job_id == user.job_id,
            Lecture.job_id == user.job_id,
            lecture_is_listed(Lecture.id),
        )
    )
    if task is None:
        raise ApiError("TASK_NOT_FOUND")
    return task


def require_lecture(session: Session, user_id: int, task: Task) -> None:
    if (
        session.scalar(
            select(LectureProgress.completed_at).where(
                LectureProgress.user_id == user_id,
                LectureProgress.lecture_id == task.lecture_id,
            )
        )
        is None
    ):
        raise ApiError("LECTURE_NOT_COMPLETED")


def materials_for(
    session: Session, task_id: int, *, include_content: bool
) -> list[dict]:
    materials = session.scalars(
        select(TaskMaterial)
        .where(TaskMaterial.task_id == task_id)
        .order_by(TaskMaterial.sort_order, TaskMaterial.id)
    )
    return [
        {
            "id": item.id,
            "title": item.title,
            **({"content": item.content} if include_content else {}),
        }
        for item in materials
    ]


def task_info(
    session: Session, task: Task, rubric: dict, *, include_content: bool
) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "situation": task.situation,
        "instruction": task.instruction,
        "materials": materials_for(session, task.id, include_content=include_content),
        "criteria": public_criteria(rubric),
        "pass_requirements": public_requirements(rubric),
    }


@router.get("", response_model=list[TaskListing])
def tasks(user: CurrentUser, session: Database):
    results = (
        select(
            Attempt.task_id,
            func.max(Attempt.total_score).label("best_score"),
            func.max(case((Attempt.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .where(Attempt.user_id == user.id, Attempt.status == "GRADED")
        .group_by(Attempt.task_id)
        .subquery()
    )
    query = (
        select(
            Task,
            Lecture,
            LectureProgress.completed_at,
            results.c.best_score,
            results.c.passed,
        )
        .join(Lecture, Task.lecture_id == Lecture.id)
        .outerjoin(
            LectureProgress,
            and_(
                LectureProgress.lecture_id == Lecture.id,
                LectureProgress.user_id == user.id,
            ),
        )
        .outerjoin(results, results.c.task_id == Task.id)
        .where(
            Task.job_id == user.job_id,
            Lecture.job_id == user.job_id,
            lecture_is_listed(Lecture.id),
        )
        .order_by(Task.sort_order, Task.id)
    )
    return [
        {
            "id": task.id,
            "title": task.title,
            "time_limit_min": task.time_limit_min,
            "required_lecture": {
                "id": lecture.id,
                "title": lecture.title,
                "completed": completed is not None,
            },
            "available": completed is not None,
            "passed": bool(passed),
            "best_score": best,
        }
        for task, lecture, completed, best, passed in session.execute(query)
    ]


@router.get("/{task_id}", response_model=TaskDetail)
def detail(task_id: int, user: CurrentUser, session: Database):
    task = own_task(session, user, task_id)
    require_lecture(session, user.id, task)
    return {
        **task_info(session, task, task.grading_rubric, include_content=False),
        "time_limit_min": task.time_limit_min,
    }
