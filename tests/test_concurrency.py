from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy.orm import Session
from support import FixtureProvider, bearer, watch_to

from app.models import Attempt, ChatMessage, Task
from app.services.grading import finish_grading


def test_different_users_can_start_at_the_same_time(client, register, clock, ready):
    _, first, task = ready
    _, _, login = register()
    second = bearer(login)
    watch_to(client, second, clock, task["required_lecture"]["id"], 480)
    barrier = Barrier(2)

    def start(headers):
        barrier.wait(timeout=5)
        return client.post(
            "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(start, (first, second)))
    assert [response.status_code for response in responses] == [201, 201]


def test_concurrent_grades_increment_level_once(
    client, application, ready, api_engine, clock
):
    user, headers, task = ready
    now = clock()
    attempt_ids = []
    with Session(api_engine) as session, session.begin():
        snapshot = session.get(Task, task["id"]).grading_rubric
        for _ in range(2):
            attempt = Attempt(
                user_id=user["id"],
                task_id=task["id"],
                status="GRADING",
                started_at=now,
                deadline_at=now,
                submitted_at=now,
                answer="Confirmed November 28.",
                grading_rubric_snapshot=snapshot,
            )
            session.add(attempt)
            session.flush()
            attempt_ids.append(attempt.id)
            session.add_all(
                [
                    ChatMessage(
                        attempt_id=attempt.id,
                        role="USER",
                        content="초안을 작성해 주세요.",
                        created_at=now,
                    ),
                    ChatMessage(
                        attempt_id=attempt.id,
                        role="ASSISTANT",
                        content="11월 28일로 작성했습니다.",
                        created_at=now,
                    ),
                    ChatMessage(
                        attempt_id=attempt.id,
                        role="USER",
                        content="일정표와 대조해 11월 28일이 확정임을 확인했습니다.",
                        created_at=now,
                    ),
                ]
            )

    barrier = Barrier(2)

    class ParallelProvider(FixtureProvider):
        def grade(self, context):
            barrier.wait(timeout=5)
            return super().grade(context)

    provider = ParallelProvider()
    factory = application.state.session_factory
    with ThreadPoolExecutor(max_workers=2) as executor:
        changes = list(
            executor.map(
                lambda attempt_id: finish_grading(factory, provider, attempt_id),
                attempt_ids,
            )
        )
    assert changes.count((user["id"], 2)) == 1
    assert changes.count(None) == 1
    paths = [f"/api/v1/attempts/{attempt_id}" for attempt_id in attempt_ids]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: client.get(paths[0], headers=headers).json(), range(2)
            )
        )
    assert sorted(result["first_view"] for result in results) == [False, True]
    assert all(
        result["passed"] and result["level_up"]["level"] == 2 for result in results
    )
