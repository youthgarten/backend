from concurrent.futures import ThreadPoolExecutor

from support import bearer


def test_attempt_start_resumes_saved_answer_and_excludes_other_users(
    client, register, clock, ready
):
    _, headers, task = ready

    def start(_):
        return client.post(
            "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(start, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    payload = next(
        response.json() for response in responses if response.status_code == 201
    )
    attempt = payload["id"]
    assert payload["remaining_sec"] == 1800
    assert payload["task"]["materials"][0]["content"]
    assert payload["task"]["pass_requirements"]["verify_min_score"] == 12
    path = f"/api/v1/attempts/{attempt}"
    saved = client.put(
        path + "/answer", headers=headers, json={"content": "초안"}
    ).json()
    clock.advance(seconds=10)
    assert (
        client.put(path + "/answer", headers=headers, json={"content": "초안"}).json()
        == saved
    )
    current = client.get("/api/v1/attempts/current", headers=headers).json()
    assert current["answer"] == "초안" and current["remaining_sec"] == 1790
    _, _, other_login = register()
    assert (
        client.get(path + "/messages", headers=bearer(other_login)).status_code == 404
    )
    assert client.delete(path, headers=headers).status_code == 204
    assert client.get("/api/v1/attempts/current", headers=headers).status_code == 204


def test_deadline_guards_writes_and_auto_submits_last_revision(client, clock, ready):
    _, headers, task = ready
    attempt = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt}"
    assert (
        client.post(
            path + "/events", headers=headers, json={"type": "FOCUS"}
        ).status_code
        == 204
    )
    assert (
        client.put(
            path + "/answer", headers=headers, json={"content": "마지막 저장"}
        ).status_code
        == 200
    )
    clock.advance(minutes=30, seconds=61)
    rejected = client.put(
        path + "/answer", headers=headers, json={"content": "유효하지 않음"}
    )
    assert (rejected.status_code, rejected.json()["error"]["code"]) == (
        400,
        "ATTEMPT_TIME_OVER",
    )
    current = client.get("/api/v1/attempts/current", headers=headers).json()
    assert current["id"] == attempt and current["auto_submitted"]
    assert (
        client.post(
            path + "/submit", headers=headers, json={"answer": "늦음"}
        ).status_code
        == 409
    )


def test_chat_provider_failure_leaves_no_messages(client, ready):
    _, headers, task = ready
    attempt = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt}/messages"
    response = client.post(path, headers=headers, json={"content": "안녕"})
    assert (response.status_code, response.json()["error"]["code"]) == (
        502,
        "AI_UNAVAILABLE",
    )
    assert client.get(path, headers=headers).json() == []


def test_ai_reply_after_deadline_is_not_saved(client, application, clock, ready):
    _, headers, task = ready
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt_id}/messages"

    class SlowProvider:
        def chat(self, messages):
            clock.advance(minutes=30, seconds=1)
            return "시간이 지난 뒤 도착한 응답"

    application.state.provider = SlowProvider()
    response = client.post(path, headers=headers, json={"content": "안녕"})
    assert (response.status_code, response.json()["error"]["code"]) == (
        400,
        "ATTEMPT_TIME_OVER",
    )
    assert client.get(path, headers=headers).json() == []


def test_manual_submit_is_allowed_at_exact_grace_boundary(client, clock, ready):
    _, headers, task = ready
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt_id}"
    saved = "마감 전에 서버에 저장한 답안"
    assert client.put(path + "/answer", headers=headers, json={"content": saved}).status_code == 200
    clock.advance(minutes=31)
    response = client.post(
        path + "/submit", headers=headers, json={"answer": "마감 뒤 바꾼 답안"}
    )
    assert response.status_code == 202
    current = client.get("/api/v1/attempts/current", headers=headers).json()
    assert current["id"] == attempt_id and not current["auto_submitted"]
    assert client.get(path, headers=headers).json()["answer"] == saved


def test_abandon_after_deadline_preserves_attempt(client, clock, ready):
    _, headers, task = ready
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    clock.advance(minutes=30, seconds=1)

    response = client.delete(f"/api/v1/attempts/{attempt_id}", headers=headers)

    assert (response.status_code, response.json()["error"]["code"]) == (
        400,
        "ATTEMPT_TIME_OVER",
    )
    assert client.get("/api/v1/attempts/current", headers=headers).json()["id"] == attempt_id
