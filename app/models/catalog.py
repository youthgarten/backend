from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKey,
    Index,
    String,
    case,
    distinct,
    func,
    literal,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.models import Base


class CatalogRelease(Base):
    __tablename__ = "catalog_release"
    __table_args__ = (
        Index("ix_catalog_release_job_key_active", "job_id", "catalog_key", "active"),
        Base.__table_args__,
    )

    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), primary_key=True)
    catalog_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lecture.id"), unique=True)
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    active: Mapped[bool] = mapped_column(Boolean)


def lecture_is_listed(lecture_id):
    return ~select(CatalogRelease.lecture_id).where(
        CatalogRelease.lecture_id == lecture_id, CatalogRelease.active.is_(False)
    ).exists()


def conceptual_pass_count(session: Session, user_id: int) -> int:
    """Count catalog versions once while keeping standalone tasks distinct."""
    from app.models import Attempt, Task

    concept = case(
        (
            CatalogRelease.lecture_id.is_not(None),
            func.concat(
                literal("catalog:"),
                CatalogRelease.job_id,
                literal(":"),
                CatalogRelease.catalog_key,
            ),
        ),
        else_=func.concat(literal("task:"), Attempt.task_id),
    )
    count = session.scalar(
        select(func.count(distinct(concept)))
        .select_from(Attempt)
        .join(Task, Task.id == Attempt.task_id)
        .outerjoin(CatalogRelease, CatalogRelease.lecture_id == Task.lecture_id)
        .where(
            Attempt.user_id == user_id,
            Attempt.status == "GRADED",
            Attempt.passed.is_(True),
        )
    )
    return int(count or 0)


def current_catalog_lecture(session: Session, lecture_id: int):
    """Resolve a released lecture to the active version of its catalog entry."""
    from app.models import Lecture

    release = session.scalar(
        select(CatalogRelease).where(CatalogRelease.lecture_id == lecture_id)
    )
    if release is None:
        return session.get(Lecture, lecture_id)
    current = session.scalar(
        select(Lecture)
        .join(CatalogRelease, CatalogRelease.lecture_id == Lecture.id)
        .where(
            CatalogRelease.job_id == release.job_id,
            CatalogRelease.catalog_key == release.catalog_key,
            CatalogRelease.active.is_(True),
        )
        .order_by(CatalogRelease.version.desc())
        .limit(1)
    )
    return current or session.get(Lecture, lecture_id)
