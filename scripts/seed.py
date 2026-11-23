import argparse
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.db import database_url, make_engine
from app.models import Job, Lecture, Task, TaskMaterial
from app.models.catalog import CatalogRelease
from scripts.publish_catalog import (
    Catalog,
    PublishedCatalog,
    catalog_content_hash,
    existing_assessment,
    has_learner_activity,
    publish_catalog,
)

JOB_NAME = "무역 사무"
LECTURE_TITLE = "AI로 영문 비즈니스 메일 쓰고 검토하기"
TASK_TITLE = "바이어 납기 지연 항의 메일 회신"
TRADE_MANIFEST = Path(__file__).resolve().parents[1] / "docs/catalog.trade-delay.json"

RUBRIC = {
    "version": "DUAL_V1",
    "task_solution_min_score": 40,
    "ai_use_min_score": 40,
    "verify_min_score": 12,
    "criterion_max_scores": {"RESULT": 50, "PROMPT": 15, "VERIFY": 20, "IMPROVE": 15},
    "required_checks": [{
        "check_id": "SHIPPING_DATE",
        "label": "확정 선적일을 정확히 안내하고 미확정 일정을 확정처럼 약속하지 않기",
        "rule": "최종 회신의 확정 선적일이 제공된 선적 일정표와 일치하고, 일정표에서 미확정으로 표시된 날짜를 확정 일정이라고 약속한 문장이 없으면 충족한다.",
    }],
}

MATERIALS = [
    ("바이어 메일 원문", "Dear team, our shipment was due November 23. Please confirm the new shipping date and explain the delay."),
    ("계약서 일부", "공급사는 지연을 확인하면 바이어에게 지연 사유와 확정 일정을 서면으로 안내한다."),
    ("선적 일정표", "| 일자 | 상태 |\n|---|---|\n| 11월 25일 | 미확정 |\n| 11월 28일 | 확정 |\n\n지연 사유는 선사의 선복 조정이다."),
]


def seed_demo(session: Session) -> None:
    """Insert missing demo content in the caller's transaction."""
    # The unique job row serializes concurrent runs before inserting children.
    statement = insert(Job).values(name=JOB_NAME)
    session.execute(statement.on_duplicate_key_update(name=JOB_NAME))
    job = session.scalars(select(Job).where(Job.name == JOB_NAME)).one()

    published = session.scalar(select(CatalogRelease.lecture_id).where(
        CatalogRelease.job_id == job.id,
        CatalogRelease.catalog_key == "shipping-delay-reply",
        CatalogRelease.active.is_(True),
    ))
    if published is not None:
        return

    lecture = session.scalars(
        select(Lecture).where(Lecture.job_id == job.id, Lecture.title == LECTURE_TITLE)
    ).one_or_none()
    if lecture is None:
        lecture = Lecture(
            job_id=job.id, title=LECTURE_TITLE, youtube_video_id="mock:trade-email",
            duration_sec=600, sort_order=1,
        )
        session.add(lecture)
        session.flush()

    task = session.scalars(select(Task).where(Task.lecture_id == lecture.id)).one_or_none()
    if task is None:
        task = Task(
            job_id=job.id, lecture_id=lecture.id, title=TASK_TITLE,
            situation="해외 바이어가 납기 지연에 항의하는 메일을 보냈다.",
            instruction="상황을 파악하고 바이어에게 보낼 영문 회신 메일을 작성한다.",
            sort_order=1, grading_rubric=RUBRIC,
        )
        session.add(task)
        session.flush()
    elif task.job_id != job.id or task.title != TASK_TITLE:
        raise ValueError("The demo lecture is already attached to a different task.")

    existing_titles = set(
        session.scalars(select(TaskMaterial.title).where(TaskMaterial.task_id == task.id))
    )
    for sort_order, (title, content) in enumerate(MATERIALS, start=1):
        if title not in existing_titles:
            session.add(TaskMaterial(
                task_id=task.id, title=title, content=content, sort_order=sort_order,
            ))
    session.flush()


def seed(session: Session) -> PublishedCatalog:
    """Publish the real course, reusing an unused legacy placeholder if present."""
    catalog = Catalog.model_validate_json(TRADE_MANIFEST.read_text(encoding="utf-8"))
    statement = insert(Job).values(name=catalog.job)
    session.execute(statement.on_duplicate_key_update(name=Job.name))
    job_id = session.scalar(select(Job.id).where(Job.name == catalog.job))
    has_release = session.scalar(select(CatalogRelease.lecture_id).where(
        CatalogRelease.job_id == job_id, CatalogRelease.catalog_key == catalog.key,
    ).limit(1))
    if has_release is not None:
        return publish_catalog(session, catalog)

    legacy = session.execute(
        select(Lecture, Task).join(Task, Task.lecture_id == Lecture.id)
        .where(
            Lecture.job_id == job_id, Lecture.title == LECTURE_TITLE,
            Lecture.youtube_video_id.in_(("mock:trade-email", "dQw4w9WgXcQ")),
            Task.job_id == job_id, Task.title == TASK_TITLE,
        ).with_for_update()
    ).one_or_none()
    if legacy is None:
        return publish_catalog(session, catalog)

    lecture, task = legacy
    if not has_learner_activity(session, lecture.id, task.id):
        return publish_catalog(session, catalog, adopt_lecture_id=lecture.id)

    # Keep the original lecture and task addressable for prior attempts without listing them again.
    version = catalog.version - 1
    archived = {
        "job": catalog.job,
        "key": catalog.key,
        "version": version,
        "lecture": {name: getattr(lecture, name) for name in catalog.lecture.model_dump()},
        "task": existing_assessment(session, task),
    }
    session.add(CatalogRelease(
        job_id=job_id, catalog_key=catalog.key, version=version, lecture_id=lecture.id,
        content_sha256=catalog_content_hash(archived), active=False,
    ))
    session.flush()
    return publish_catalog(session, catalog)


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the trade-office lecture and assessment.")
    parser.add_argument("--demo", action="store_true", help="Seed the mock lecture for development tests.")
    args = parser.parse_args()
    engine = make_engine(database_url())
    try:
        with Session(engine) as session, session.begin():
            if args.demo:
                seed_demo(session)
            else:
                release = seed(session)
    finally:
        engine.dispose()
    if args.demo:
        print("Checked the demo lecture and assessment.")
    else:
        print(f"Published lecture {release.lecture_id} and task {release.task_id}.")


if __name__ == "__main__":
    main()
