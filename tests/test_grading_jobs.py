from sqlalchemy.orm import Session

from app.models import Attempt
from app.services.grading_jobs import LEASE, MAX_ATTEMPTS, claim, owns_lease


def test_expired_grading_lease_is_fenced_and_retries_are_bounded(
    client, application, ready, api_engine, clock, monkeypatch
):
    _, headers, task = ready
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    monkeypatch.setattr("app.api.attempts.enqueue", lambda *args: None)
    assert client.post(
        f"/api/v1/attempts/{attempt_id}/submit",
        headers=headers,
        json={"answer": "답안"},
    ).status_code == 202
    factory = application.state.session_factory

    first = claim(factory, attempt_id, clock())
    assert first is not None
    assert claim(factory, attempt_id, clock()) is None
    clock.advance(seconds=int(LEASE.total_seconds()) + 1)
    second = claim(factory, attempt_id, clock())
    assert second is not None and second != first
    with Session(api_engine) as session:
        assert owns_lease(session, attempt_id, first, clock()) is None
        assert owns_lease(session, attempt_id, second, clock()) is not None

    for _ in range(MAX_ATTEMPTS - 2):
        clock.advance(seconds=int(LEASE.total_seconds()) + 1)
        assert claim(factory, attempt_id, clock()) is not None
    clock.advance(seconds=int(LEASE.total_seconds()) + 1)
    assert claim(factory, attempt_id, clock()) is None
    with Session(api_engine) as session:
        attempt = session.get(Attempt, attempt_id)
        assert attempt.status == "GRADING_FAILED"
        assert attempt.grading_failure == "grading retries exhausted"
