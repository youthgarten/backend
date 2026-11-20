from concurrent.futures import ThreadPoolExecutor
from threading import Event

from support import FixtureProvider


def start_path(client, headers, task):
    started = client.post("/api/v1/attempts", headers=headers, json={"task_id": task["id"]})
    assert started.status_code == 201
    return f"/api/v1/attempts/{started.json()['id']}"


def test_chat_idempotency_reuses_turn_and_rejects_changed_payload(client, application, ready):
    _, headers, task = ready
    path = start_path(client, headers, task)

    class Provider(FixtureProvider):
        calls = 0

        def chat(self, messages):
            self.calls += 1
            return super().chat(messages)

    provider = application.state.provider = Provider()
    keyed = {**headers, "Idempotency-Key": "verify-28"}
    first = client.post(path + "/messages", headers=keyed, json={"content": "선적일은 언제인가요?"})
    repeated = client.post(path + "/messages", headers=keyed, json={"content": "선적일은 언제인가요?"})
    changed = client.post(path + "/messages", headers=keyed, json={"content": "자료는 무엇인가요?"})
    assert first.status_code == repeated.status_code == 201
    assert first.json() == repeated.json() and provider.calls == 1
    assert changed.status_code == 409
    assert len(client.get(path + "/messages", headers=headers).json()) == 2


def test_two_concurrent_chat_requests_do_not_read_the_same_history(client, application, ready):
    _, headers, task = ready
    path = start_path(client, headers, task)
    entered = Event()
    release = Event()

    class SlowProvider(FixtureProvider):
        def chat(self, messages):
            if messages[-1]["content"] == "첫 질문":
                entered.set()
                assert release.wait(timeout=5)
            return super().chat(messages)

    application.state.provider = SlowProvider()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(client.post, path + "/messages", headers=headers, json={"content": "첫 질문"})
        try:
            assert entered.wait(timeout=5)
            second = client.post(path + "/messages", headers=headers, json={"content": "두 번째 질문"})
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "CHAT_IN_PROGRESS"
        finally:
            release.set()
        assert running.result(timeout=5).status_code == 201


def test_late_autosave_with_old_revision_does_not_overwrite_newer_answer(client, ready):
    _, headers, task = ready
    path = start_path(client, headers, task)
    first = client.put(
        path + "/answer", headers=headers, json={"content": "처음", "expected_revision_id": None}
    )
    new = client.put(path + "/answer", headers=headers, json={
        "content": "최신", "expected_revision_id": first.json()["revision_id"],
    })
    stale = client.put(path + "/answer", headers=headers, json={
        "content": "늦게 도착한 이전 답안", "expected_revision_id": first.json()["revision_id"],
    })
    assert new.status_code == 200 and stale.status_code == 409
    current = client.get("/api/v1/attempts/current", headers=headers).json()
    assert current["answer"] == "최신"
    assert current["answer_revision_id"] == new.json()["revision_id"]


def test_combined_history_budget_rejects_draft_before_it_prevents_grading(
    client, application, ready, monkeypatch
):
    from app.services import input_limits

    _, headers, task = ready
    path = start_path(client, headers, task)
    monkeypatch.setattr(input_limits, "MAX_HISTORY_BYTES", 1000)
    application.state.provider = FixtureProvider()
    assert client.post(path + "/messages", headers=headers, json={"content": "질문"}).status_code == 201
    assert client.put(path + "/answer", headers=headers, json={"content": "가" * 100}).status_code == 200
    blocked = client.put(path + "/answer", headers=headers, json={"content": "가" * 300})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "ATTEMPT_INPUT_LIMIT"
