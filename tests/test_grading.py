import pytest
from sqlalchemy.orm import Session
from support import FixtureProvider, submit_with_evidence

from app.models import Attempt, Task


@pytest.mark.parametrize(
    ("points", "required", "expected"),
    [
        ((40, 14, 12, 14), True, (80, True, True, True)),
        ((45, 15, 11, 14), True, (85, True, False, False)),
        ((50, 15, 20, 15), False, (100, False, True, False)),
    ],
)
def test_grade_dual_gates_and_result_contract(
    client, application, ready, points, required, expected
):
    _, headers, task = ready
    application.state.provider = FixtureProvider(points, required)
    path = submit_with_evidence(client, headers, task["id"])
    result = client.get(path, headers=headers).json()
    assert result["status"] == "GRADED"
    assert (
        result["total_score"],
        result["dimensions"][0]["passed"],
        result["dimensions"][1]["passed"],
        result["passed"],
    ) == expected
    assert result["first_view"]
    assert not client.get(path, headers=headers).json()["first_view"]
    tasks = client.get("/api/v1/tasks", headers=headers).json()
    assert tasks[0]["best_score"] == expected[0] and tasks[0]["passed"] == expected[-1]
    past = client.get("/api/v1/attempts", headers=headers).json()
    assert past["total"] == 1 and past["items"][0]["passed"] == expected[-1]
    if result["passed"]:
        path = submit_with_evidence(client, headers, task["id"])
        repeat = client.get(path, headers=headers).json()
        assert repeat["level_up"] == {"leveled_up": False, "level": 2}


def test_model_evidence_invalid_twice_then_retry_uses_snapshot(
    client, application, ready, api_engine
):
    _, headers, task = ready
    provider = FixtureProvider(invalid=True)
    application.state.provider = provider
    path = submit_with_evidence(client, headers, task["id"])
    attempt_id = int(path.rsplit("/", 1)[-1])
    failed = client.get(path, headers=headers).json()
    assert failed["status"] == "GRADING_FAILED" and failed["scores"] is None
    assert len(provider.inputs) == 2
    original = None
    try:
        with Session(api_engine) as session, session.begin():
            row = session.get(Task, task["id"])
            original = row.grading_rubric
            row.grading_rubric = {**original, "verify_min_score": 20}
        provider.invalid = False
        response = client.post(path + "/regrade", headers=headers)
        assert response.status_code == 202
        with Session(api_engine) as session:
            assert (
                session.get(Attempt, attempt_id).grading_rubric_snapshot[
                    "verify_min_score"
                ]
                == 12
            )
        result = client.get(path, headers=headers).json()
        assert (
            result["passed"] and result["pass_requirements"]["verify_min_score"] == 12
        )
    finally:
        if original is not None:
            with Session(api_engine) as session, session.begin():
                session.get(Task, task["id"]).grading_rubric = original


def test_revision_based_verification_feedback_includes_the_record(
    client, application, ready
):
    _, headers, task = ready

    class RevisionProvider(FixtureProvider):
        def grade(self, context):
            output = super().grade(context)
            verify = next(
                item for item in output["scores"] if item["criterion"] == "VERIFY"
            )
            verify["evidence_message_id"] = None
            verify["evidence_revision_id"] = context["revisions"][0]["id"]
            return output

    application.state.provider = RevisionProvider()
    response = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    )
    path = f"/api/v1/attempts/{response.json()['id']}"
    assert client.post(
        path + "/messages", headers=headers, json={"content": "회신 초안을 작성해 주세요."}
    ).status_code == 201
    note = "선적 일정표에서 11월 28일 확정 여부를 대조했습니다."
    assert (
        client.put(
            path + "/answer", headers=headers, json={"content": note}
        ).status_code
        == 200
    )
    assert (
        client.post(
            path + "/submit", headers=headers, json={"answer": "Confirmed November 28."}
        ).status_code
        == 202
    )
    result = client.get(path, headers=headers).json()
    verify = next(item for item in result["scores"] if item["criterion"] == "VERIFY")
    assert verify["evidence_message_id"] is None
    assert note in verify["feedback"]
