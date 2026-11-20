from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentUser, Database
from app.api.schemas import Member
from app.models import Job, User
from app.models.catalog import conceptual_pass_count

router = APIRouter(prefix="/users", tags=["users"])


class Tutorial(BaseModel):
    tutorial_seen: Literal[True]


def profile(user: User, job: Job):
    return {
        "id": user.id,
        "login_id": user.login_id,
        "nickname": user.nickname,
        "job": {"id": job.id, "name": job.name},
        "level": user.level,
    }


def passed_count(session: Database, user_id: int) -> int:
    return conceptual_pass_count(session, user_id)


@router.get("/me", response_model=Member)
def me(user: CurrentUser, session: Database):
    return {
        **profile(user, session.get(Job, user.job_id)),
        "passed_task_count": passed_count(session, user.id),
        "tutorial_seen": user.tutorial_seen,
    }


@router.patch("/me", response_model=Member)
def complete_tutorial(body: Tutorial, user: CurrentUser, session: Database):
    user.tutorial_seen = body.tutorial_seen
    session.commit()
    return me(user, session)
