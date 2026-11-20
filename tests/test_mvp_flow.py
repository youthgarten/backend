import json
import re
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import anyio
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from support import FixtureProvider, bearer, report, submit_with_evidence, watch_to

from app.main import create_app
from app.models import Attempt, AttemptScore, GradingJob, Task, User
from app.services import grading
from app.services.ai import ChatCompletionsProvider


def test_end_to_end_using_the_real_http_adapter(client, register, clock, application):
    calls = []
    fixture = FixtureProvider()

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, message))
            if "response_format" in message:
                context = json.loads(message["messages"][1]["content"])
                if message["response_format"]["json_schema"]["name"] == "task_solution":
                    content = json.dumps(
                        {
                            "score": 40,
                            "feedback": "실제 제출 답안 평가",
                            "required_checks": [
                                {
                                    "check_id": "SHIPPING_DATE",
                                    "met": True,
                                    "feedback": "확정일 근거",
                                }
                            ],
                            "summary_feedback": "최신 일정표를 다시 확인하세요.",
                        }
                    )
                else:
                    scored = fixture.grade(context)["scores"]
                    verify = next(row for row in scored if row["criterion"] == "VERIFY")
                    verify["evidence_message_id"] = None
                    verify["evidence_revision_id"] = context["revisions"][-1]["id"]
                    content = json.dumps(
                        {
                            row["criterion"]: {
                                **{
                                    key: value
                                    for key, value in row.items()
                                    if key != "criterion"
                                },
                                "evidence_revision_id": row.get("evidence_revision_id"),
                            }
                            for row in scored
                            if row["criterion"] != "RESULT"
                        }
                    )
            else:
                content = fixture.chat(message["messages"])
            result = json.dumps(
                {"choices": [{"message": {"content": content}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        application.state.provider = ChatCompletionsProvider(
            f"http://127.0.0.1:{server.server_port}/v1", "fixture-model", None
        )
        profile, _, login = register()
        headers = bearer(login)
        task = client.get("/api/v1/tasks", headers=headers).json()[0]
        lesson = task["required_lecture"]["id"]
        watch_to(client, headers, clock, lesson, 479)
        body = {"task_id": task["id"]}
        assert (
            client.post("/api/v1/attempts", headers=headers, json=body).status_code
            == 403
        )
        clock.advance(seconds=1)
        assert report(client, headers, lesson, [[479, 480]], 480).json()["completed"]

        attempt = client.post("/api/v1/attempts", headers=headers, json=body).json()[
            "id"
        ]
        path = f"/api/v1/attempts/{attempt}"
        assert (
            client.put(
                path + "/answer", headers=headers, json={"content": "draft"}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/v1/attempts/current", headers=headers).json()["answer"]
            == "draft"
        )
        assert (
            client.post(
                path + "/messages",
                headers=headers,
                json={
                    "content": "선적 일정표에서 11월 28일이 확정임을 확인했습니다.",
                },
            ).status_code
            == 201
        )
        assert client.put(
            path + "/answer",
            headers=headers,
            json={"content": "draft checked against the schedule"},
        ).status_code == 200
        assert (
            client.post(
                path + "/submit",
                headers=headers,
                json={"answer": "Confirmed November 28."},
            ).status_code
            == 202
        )
        result = client.get(path, headers=headers).json()
        assert result["passed"] and result["level_up"] == {
            "leveled_up": True,
            "level": 2,
        }
        assert result["total_score"] == 80 and len(result["scores"]) == 4
        assert (
            client.get("/api/v1/users/me", headers=headers).json()["id"]
            == profile["id"]
        )
        assert [call[0] for call in calls] == [
            "/v1/chat/completions",
            "/v1/chat/completions",
            "/v1/chat/completions",
        ]
        assert calls[1][1]["response_format"]["type"] == "json_schema"
        assert (
            json.loads(calls[1][1]["messages"][1]["content"])["submitted_answer"]
            == "Confirmed November 28."
        )
        assert fixture.inputs[0]["revisions"][-1]["content"] == "draft checked against the schedule"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_openapi_covers_the_documented_http_contract(client, application):
    contract = """GET /jobs
POST /auth/signup
POST /auth/login
POST /auth/refresh
POST /auth/logout
GET /users/me
PATCH /users/me
GET /lectures
GET /lectures/{id}
POST /lectures/{id}/progress
GET /tasks
GET /tasks/{id}
POST /attempts
GET /attempts/current
GET /attempts
GET /attempts/{id}
DELETE /attempts/{id}
POST /attempts/{id}/messages
GET /attempts/{id}/messages
PUT /attempts/{id}/answer
POST /attempts/{id}/events
POST /attempts/{id}/submit
POST /attempts/{id}/regrade""".splitlines()

    def canonical(path):
        return re.sub(r"\{[^}]+\}", "{id}", path)

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    implemented = {
        (method.upper(), canonical(path))
        for path, methods in paths.items()
        if path.startswith("/api/v1/")
        for method in methods
    }
    assert {
        f"{method} {path.removeprefix('/api/v1')}" for method, path in implemented
    } == set(contract)
    for path, methods in paths.items():
        if path.startswith("/api/v1/"):
            for method in methods.values():
                for code, result in method["responses"].items():
                    if code in {"200", "201", "202"}:
                        assert result["content"]["application/json"]["schema"]
    assert schema["components"]["schemas"]["ResultDetail"]["properties"]["dimensions"]
    with client.websocket_connect("/api/v1/ws/map") as socket:
        socket.send_json({"type": "auth", "access_token": "invalid"})
        assert socket.receive()["code"] == 4401


def test_auto_submit_and_manual_submit_compete_once(client, application, clock, ready):
    _, headers, task = ready
    provider = FixtureProvider(points=(45, 15, 0, 15))
    application.state.provider = provider
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt_id}"
    assert (
        client.put(
            path + "/answer", headers=headers, json={"content": "저장된 답안"}
        ).status_code
        == 200
    )
    clock.advance(minutes=31, seconds=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        lookup = executor.submit(
            client.get, "/api/v1/attempts/current", headers=headers
        )
        late = executor.submit(
            client.post, path + "/submit", headers=headers, json={"answer": "늦음"}
        )
        assert lookup.result().json()["auto_submitted"]
        assert late.result().status_code == 409
    assert len(provider.inputs) == 1
    assert provider.inputs[0]["answer"] == "저장된 답안"


def test_attempt_freezes_task_content_for_grading(client, application, ready, api_engine):
    _, headers, task = ready
    application.state.provider = FixtureProvider(points=(40, 15, 0, 15))
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    try:
        with Session(api_engine) as session, session.begin():
            stored = session.get(Attempt, attempt_id)
            assert stored.task_snapshot["title"] == task["title"]
            session.get(Task, task["id"]).title = "나중에 바뀐 과제명"
        response = client.post(
            f"/api/v1/attempts/{attempt_id}/submit",
            headers=headers,
            json={"answer": "Confirmed November 28."},
        )
        assert response.status_code == 202
        assert application.state.provider.inputs[0]["task"]["title"] == task["title"]
    finally:
        with Session(api_engine) as session, session.begin():
            session.get(Task, task["id"]).title = task["title"]


def test_submitting_persists_grading_job(client, ready, api_engine, monkeypatch):
    _, headers, task = ready
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    monkeypatch.setattr("app.api.attempts.enqueue", lambda *args: None)

    response = client.post(
        f"/api/v1/attempts/{attempt_id}/submit", headers=headers, json={"answer": "답안"}
    )

    assert response.status_code == 202
    with Session(api_engine) as session:
        assert session.get(GradingJob, attempt_id) is not None


def test_worker_auto_submits_without_an_attempt_request(client, application, ready, clock):
    _, headers, task = ready
    provider = FixtureProvider(points=(45, 15, 0, 15))
    application.state.provider = provider
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt_id}"
    assert client.put(
        path + "/answer", headers=headers, json={"content": "마지막 저장 답안"}
    ).status_code == 200
    clock.advance(minutes=31, seconds=1)

    anyio.run(application.state.grading_worker.run_once)

    result = client.get(path, headers=headers).json()
    assert result["status"] == "GRADED"
    assert result["answer"] == "마지막 저장 답안"
    assert len(provider.inputs) == 1


def test_worker_retries_transient_provider_failure(client, application, ready, clock):
    _, headers, task = ready

    class Unavailable:
        def grade(self, _context):
            raise grading.ProviderError("temporarily unavailable")

    application.state.provider = Unavailable()
    attempt_id = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt_id}"
    response = client.post(path + "/submit", headers=headers, json={"answer": "답안"})
    assert response.status_code == 202
    assert client.get(path, headers=headers).json()["status"] == "GRADING"

    application.state.provider = FixtureProvider(points=(40, 15, 0, 15))
    clock.advance(seconds=5)
    anyio.run(application.state.grading_worker.run_once)

    assert client.get(path, headers=headers).json()["status"] == "GRADED"


def test_failed_grade_save_rolls_back_and_startup_recovers_inflight(
    client,
    application,
    ready,
    api_engine,
    monkeypatch,
    clock,
):
    user, headers, task = ready
    application.state.provider = FixtureProvider()
    save_grade = grading.save_grade

    def failed_save(*arguments):
        save_grade(*arguments)
        raise RuntimeError("Simulate failure before commit.")

    monkeypatch.setattr(grading, "save_grade", failed_save)
    path = submit_with_evidence(client, headers, task["id"])
    attempt_id = int(path.rsplit("/", 1)[-1])
    failed = client.get(path, headers=headers).json()
    assert failed["status"] == "GRADING_FAILED" and failed["scores"] is None
    with Session(api_engine) as session, session.begin():
        assert session.get(AttemptScore, (attempt_id, "RESULT")) is None
        assert session.get(User, user["id"]).level == 1
        session.get(Attempt, attempt_id).status = "GRADING"

    recovered = create_app(
        api_engine, clock=clock, jwt_secret=application.state.jwt_secret
    )
    with TestClient(recovered) as restored:
        assert restored.get(path, headers=headers).json()["status"] == "GRADING_FAILED"
