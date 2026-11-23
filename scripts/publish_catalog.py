"""Publish a validated lecture and its linked assessment from a JSON manifest."""

import argparse
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.db import database_url, make_engine
from app.models import Attempt, Job, Lecture, LectureProgress, Task, TaskMaterial
from app.models.catalog import CatalogRelease
from app.services.input_limits import MAX_CATALOG_BYTES, json_size

Title = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
SortOrder = Annotated[int, Field(ge=1, le=10000)]
Score = Annotated[int, Field(ge=1, le=100)]
Criterion = Literal["RESULT", "PROMPT", "VERIFY", "IMPROVE"]


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class RequiredCheck(ManifestModel):
    check_id: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
    label: Title
    rule: Description


class Rubric(ManifestModel):
    version: Literal["DUAL_V1"]
    task_solution_min_score: Score
    ai_use_min_score: Score
    verify_min_score: Score
    criterion_max_scores: dict[Criterion, Score]
    required_checks: Annotated[list[RequiredCheck], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def consistent_scores(self):
        maxima = self.criterion_max_scores
        if set(maxima) != {"RESULT", "PROMPT", "VERIFY", "IMPROVE"} or sum(maxima.values()) != 100:
            raise ValueError("The four criterion maxima must add up to 100.")
        if (
            self.task_solution_min_score > maxima["RESULT"]
            or self.ai_use_min_score > 100 - maxima["RESULT"]
            or self.verify_min_score > maxima["VERIFY"]
        ):
            raise ValueError("Each threshold must fit its rubric maximum.")
        if len({check.check_id for check in self.required_checks}) != len(self.required_checks):
            raise ValueError("Required check IDs must be unique within the task.")
        return self


class CatalogLecture(ManifestModel):
    title: Title
    youtube_video_id: Annotated[
        str, StringConstraints(pattern=r"^(?:[A-Za-z0-9_-]{11}|local:[a-z0-9][a-z0-9-]{0,13})$")
    ]
    duration_sec: Annotated[int, Field(ge=1, le=86400)]
    sort_order: SortOrder


class CatalogMaterial(ManifestModel):
    title: Title
    content: Description


class CatalogTask(ManifestModel):
    title: Title
    situation: Description
    instruction: Description
    time_limit_min: Annotated[int, Field(ge=1, le=180)]
    materials: Annotated[list[CatalogMaterial], Field(min_length=1, max_length=12)]
    rubric: Rubric

    @model_validator(mode="after")
    def fits_model_input(self):
        if json_size(self.model_dump()) > MAX_CATALOG_BYTES:
            raise ValueError("Task and materials exceed the model input budget.")
        return self


class Catalog(ManifestModel):
    job: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    version: Annotated[int, Field(ge=1, le=2147483647)]
    lecture: CatalogLecture
    task: CatalogTask


class PublishConflict(ValueError):
    pass


@dataclass(frozen=True)
class PublishedCatalog:
    lecture_id: int
    task_id: int


def catalog_content_hash(content: dict) -> str:
    serialized = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return sha256(serialized.encode()).hexdigest()


def has_learner_activity(session: Session, lecture_id: int, task_id: int) -> bool:
    history = (
        select(LectureProgress.lecture_id).where(LectureProgress.lecture_id == lecture_id),
        select(Attempt.id).where(Attempt.task_id == task_id),
    )
    return any(session.scalar(query.limit(1).with_for_update()) is not None for query in history)


def existing_assessment(session: Session, task: Task) -> dict:
    materials = session.scalars(
        select(TaskMaterial).where(TaskMaterial.task_id == task.id)
        .order_by(TaskMaterial.sort_order, TaskMaterial.id)
        .with_for_update()
    )
    return {
        "title": task.title,
        "situation": task.situation,
        "instruction": task.instruction,
        "time_limit_min": task.time_limit_min,
        "rubric": task.grading_rubric,
        "materials": [{"title": m.title, "content": m.content} for m in materials],
    }


def adopt_unstarted_lecture(
    session: Session, catalog: Catalog, job_id: int, lecture_id: int
) -> tuple[Lecture, Task]:
    existing = session.execute(
        select(Lecture, Task)
        .join(Task, Task.lecture_id == Lecture.id)
        .where(Lecture.id == lecture_id, Lecture.job_id == job_id, Task.job_id == job_id)
        .with_for_update()
    ).one_or_none()
    if existing is None:
        raise PublishConflict("Existing lecture and assessment must belong to the catalog job.")
    lecture, task = existing
    released = session.scalar(
        select(CatalogRelease.lecture_id).where(CatalogRelease.lecture_id == lecture.id)
        .with_for_update()
    )
    if released is not None or has_learner_activity(session, lecture.id, task.id):
        raise PublishConflict("The existing lecture is already released or has learner activity.")

    if existing_assessment(session, task) != catalog.task.model_dump() or task.sort_order != catalog.lecture.sort_order:
        raise PublishConflict("The existing assessment must match the catalog.")
    for field, value in catalog.lecture.model_dump().items():
        setattr(lecture, field, value)
    return lecture, task


def publish_catalog(
    session: Session, catalog: Catalog, *, adopt_lecture_id: int | None = None
) -> PublishedCatalog:
    """Publish atomically in the caller's transaction."""
    content_hash = catalog_content_hash(catalog.model_dump())

    # A job row serializes publishing across concurrent importers for that job.
    statement = insert(Job).values(name=catalog.job)
    session.execute(statement.on_duplicate_key_update(name=Job.name))
    job_id = session.scalar(select(Job.id).where(Job.name == catalog.job))
    previous = session.get(CatalogRelease, (job_id, catalog.key, catalog.version))
    if previous is not None:
        if previous.content_sha256 != content_hash:
            raise PublishConflict("Released content is immutable. Use a new version.")
        if adopt_lecture_id is not None and adopt_lecture_id != previous.lecture_id:
            raise PublishConflict("This release is already associated with another lecture.")
        task_id = session.scalar(select(Task.id).where(Task.lecture_id == previous.lecture_id))
        return PublishedCatalog(previous.lecture_id, task_id)

    latest = session.scalar(
        select(CatalogRelease)
        .where(CatalogRelease.job_id == job_id, CatalogRelease.catalog_key == catalog.key)
        .order_by(CatalogRelease.version.desc())
        .limit(1)
    )
    if latest is not None and catalog.version <= latest.version:
        raise PublishConflict("The new version must be greater than the current version.")

    if adopt_lecture_id is not None:
        if latest is not None:
            raise PublishConflict("Adoption is available for the first release only.")
        lecture, task = adopt_unstarted_lecture(session, catalog, job_id, adopt_lecture_id)
    else:
        lecture = Lecture(job_id=job_id, **catalog.lecture.model_dump())
        session.add(lecture)
        session.flush()
        task = Task(
            job_id=job_id, lecture_id=lecture.id, title=catalog.task.title,
            situation=catalog.task.situation, instruction=catalog.task.instruction,
            time_limit_min=catalog.task.time_limit_min, sort_order=lecture.sort_order,
            grading_rubric=catalog.task.rubric.model_dump(),
        )
        session.add(task)
        session.flush()
        session.add_all(
            TaskMaterial(task_id=task.id, sort_order=position, **material.model_dump())
            for position, material in enumerate(catalog.task.materials, start=1)
        )
    if latest is not None:
        latest.active = False
    session.add(CatalogRelease(
        job_id=job_id, catalog_key=catalog.key, version=catalog.version,
        lecture_id=lecture.id, content_sha256=content_hash, active=True,
    ))
    session.flush()
    return PublishedCatalog(lecture.id, task.id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--adopt-unstarted-lecture", type=int, metavar="LECTURE_ID")
    args = parser.parse_args()
    catalog = Catalog.model_validate_json(args.manifest.read_text(encoding="utf-8-sig"))
    if args.validate_only:
        print(f"Validated {catalog.key} v{catalog.version}.")
        return

    engine = make_engine(database_url())
    try:
        with Session(engine) as session, session.begin():
            release = publish_catalog(session, catalog, adopt_lecture_id=args.adopt_unstarted_lecture)
        print(f"Published lecture {release.lecture_id} and task {release.task_id}.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
