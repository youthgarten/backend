from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, TINYINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "pk": "pk_%(table_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    })
    __table_args__: ClassVar[dict[str, str]] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    login_id: Mapped[str] = mapped_column(String(30), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"))
    level: Mapped[int] = mapped_column(TINYINT, server_default=text("1"))
    tutorial_seen: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Lecture(Base):
    __tablename__ = "lecture"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"))
    title: Mapped[str] = mapped_column(String(100))
    youtube_video_id: Mapped[str] = mapped_column(String(20))
    duration_sec: Mapped[int]
    sort_order: Mapped[int]


class LectureProgress(Base):
    __tablename__ = "lecture_progress"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lecture.id"), primary_key=True)
    watched_ranges: Mapped[list[list[int]]] = mapped_column(JSON(none_as_null=True))
    watched_sec: Mapped[int]
    last_position_sec: Mapped[int]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    watch_budget_used_until: Mapped[datetime | None] = mapped_column(DateTime)


class Task(Base):
    __tablename__ = "task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"))
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lecture.id"), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    situation: Mapped[str] = mapped_column(Text)
    instruction: Mapped[str] = mapped_column(Text)
    time_limit_min: Mapped[int] = mapped_column(Integer, server_default=text("30"))
    sort_order: Mapped[int]
    grading_rubric: Mapped[dict[str, Any]] = mapped_column(JSON(none_as_null=True))


class TaskMaterial(Base):
    __tablename__ = "task_material"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    title: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int]


class Attempt(Base):
    __tablename__ = "attempt"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Up to 20,000 characters can exceed TEXT's byte limit in utf8mb4.
    answer: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    grading_rubric_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON(none_as_null=True))
    task_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    required_check_results: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    total_score: Mapped[int | None]
    passed: Mapped[bool | None]
    leveled_up: Mapped[bool | None]
    summary_feedback: Mapped[str | None] = mapped_column(Text)
    grading_model: Mapped[str | None] = mapped_column(String(100))
    grading_prompt_version: Mapped[str | None] = mapped_column(String(32))
    grading_failure: Mapped[str | None] = mapped_column(String(100))
    result_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    in_progress_uid: Mapped[int | None] = mapped_column(
        BigInteger, Computed("IF(status = 'IN_PROGRESS', user_id, NULL)"), unique=True
    )


class RefreshToken(Base):
    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    family_id: Mapped[str] = mapped_column(CHAR(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempt.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AnswerRevision(Base):
    __tablename__ = "answer_revision"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempt.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(MEDIUMTEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    after_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_message.id", ondelete="SET NULL")
    )


class AttemptEvent(Base):
    __tablename__ = "attempt_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempt.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AttemptScore(Base):
    __tablename__ = "attempt_score"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    criterion: Mapped[str] = mapped_column(String(20), primary_key=True)
    score: Mapped[int]
    feedback: Mapped[str] = mapped_column(Text)
    evidence_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_message.id", ondelete="SET NULL")
    )
    evidence_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("answer_revision.id", ondelete="SET NULL")
    )


class GradingJob(Base):
    __tablename__ = "grading_job"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    lease_token: Mapped[str | None] = mapped_column(CHAR(36))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(100))
