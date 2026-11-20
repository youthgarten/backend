from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import Database
from app.api.schemas import JobView
from app.models import Job

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_model=list[JobView])
def jobs(session: Database):
    return [
        {"id": item.id, "name": item.name}
        for item in session.scalars(select(Job).order_by(Job.id))
    ]
