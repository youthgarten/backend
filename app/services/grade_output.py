from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictInt

Feedback = Annotated[str, Field(min_length=1, max_length=8000)]


class ScoredFeedback(BaseModel):
    score: StrictInt
    feedback: Feedback


class ProcessScore(ScoredFeedback):
    evidence_message_id: StrictInt | None = None
    evidence_revision_id: StrictInt | None = None


class Score(ProcessScore):
    criterion: str


class RequiredCheck(BaseModel):
    check_id: str
    met: StrictBool
    feedback: Feedback


class ResultAssessment(ScoredFeedback):
    required_checks: list[RequiredCheck]
    summary_feedback: Feedback


class ProcessAssessment(BaseModel):
    PROMPT: ProcessScore
    VERIFY: ProcessScore
    IMPROVE: ProcessScore


class GradeOutput(BaseModel):
    scores: list[Score]
    required_checks: list[RequiredCheck]
    summary_feedback: Feedback


def grading_response_format(assessment: type[BaseModel], name: str) -> dict:
    schema = assessment.model_json_schema()
    for model in (schema, *schema["$defs"].values()):
        properties = model["properties"]
        model["additionalProperties"] = False
        model["required"] = list(properties)
        for key, definition in properties.items():
            definition.pop("default", None)
            if key in {"feedback", "summary_feedback"}:
                definition["maxLength"] = 160 if key == "summary_feedback" else 80
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }
