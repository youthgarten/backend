from typing import Literal

from pydantic import BaseModel

AttemptStatus = Literal["IN_PROGRESS", "GRADING", "GRADED", "GRADING_FAILED"]
ResultStatus = Literal["GRADING", "GRADED", "GRADING_FAILED"]
DimensionCode = Literal["TASK_SOLUTION", "AI_USE"]
CriterionCode = Literal["RESULT", "PROMPT", "VERIFY", "IMPROVE"]


class JobView(BaseModel):
    id: int
    name: str


class Profile(BaseModel):
    id: int
    login_id: str
    nickname: str
    job: JobView
    level: int


class Member(Profile):
    passed_task_count: int
    tutorial_seen: bool


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int


class Progress(BaseModel):
    progress_rate: float
    completed: bool


class ProgressSaved(Progress):
    just_completed: bool


class Title(BaseModel):
    id: int
    title: str


class LectureView(Progress, Title):
    duration_sec: int


class LectureDetail(LectureView):
    youtube_video_id: str
    last_position_sec: int
    task_id: int | None


class LectureDashboard(BaseModel):
    lectures: list[LectureView]
    completed_count: int
    total_count: int


class RequiredLecture(Title):
    completed: bool


class TaskListing(Title):
    time_limit_min: int
    required_lecture: RequiredLecture
    available: bool
    passed: bool
    best_score: int | None


class Criterion(BaseModel):
    code: CriterionCode
    name: str
    dimension: DimensionCode
    max_score: int


class PublicCheck(BaseModel):
    check_id: str
    label: str


class DualRequirements(BaseModel):
    version: Literal["DUAL_V1"]
    task_solution_min_score: int
    ai_use_min_score: int
    verify_min_score: int
    required_checks: list[PublicCheck]


class LegacyRequirements(BaseModel):
    version: Literal["LEGACY_TOTAL_V1"]
    min_total_score: int


PassRequirements = DualRequirements | LegacyRequirements


class TaskPresentation(Title):
    situation: str
    instruction: str
    criteria: list[Criterion]
    pass_requirements: PassRequirements


class TaskDetail(TaskPresentation):
    time_limit_min: int
    materials: list[Title]


class Material(Title):
    content: str


class AttemptTask(TaskPresentation):
    materials: list[Material]


class AttemptStarted(BaseModel):
    id: int
    status: Literal["IN_PROGRESS"]
    started_at: str
    deadline_at: str
    remaining_sec: int
    task: AttemptTask


class CurrentAttempt(AttemptStarted):
    answer: str
    answer_revision_id: int | None


class ResultNotice(BaseModel):
    id: int
    status: ResultStatus
    auto_submitted: bool


class InGrading(BaseModel):
    id: int
    status: Literal["GRADING"]


class ChatLine(BaseModel):
    id: int
    role: Literal["USER", "ASSISTANT"]
    content: str
    created_at: str


class ChatTurn(BaseModel):
    user_message: ChatLine
    assistant_message: ChatLine


class SavedAnswer(BaseModel):
    saved_at: str
    revision_id: int


class Dimension(BaseModel):
    code: DimensionCode
    name: str
    score: int
    max_score: int
    min_score: int
    passed: bool


class CheckedRequirement(PublicCheck):
    met: bool
    feedback: str


class ScoredCriterion(BaseModel):
    criterion: CriterionCode
    name: str
    dimension: DimensionCode
    score: int
    max_score: int
    feedback: str
    evidence_message_id: int | None
    evidence_revision_id: int | None = None


class LevelChange(BaseModel):
    leveled_up: bool
    level: int


class ResultEnvelope(BaseModel):
    id: int
    task: Title
    submitted_at: str | None
    total_score: int | None
    passed: bool | None


class ResultListing(ResultEnvelope):
    status: ResultStatus


class ResultsPage(BaseModel):
    items: list[ResultListing]
    page: int
    size: int
    total: int


class ResultDetail(ResultEnvelope):
    status: AttemptStatus
    started_at: str
    answer: str | None
    content_snapshot_status: Literal["FROZEN", "LEGACY_LIVE"]
    summary_feedback: str | None
    pass_requirements: PassRequirements
    dimensions: list[Dimension] | None
    required_checks: list[CheckedRequirement] | None
    scores: list[ScoredCriterion] | None
    recommended_lecture: Title
    level_up: LevelChange
    first_view: bool
