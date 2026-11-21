"""Run synthetic grading cases through this backend and a configured real model."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import database_url, make_engine
from app.main import create_app
from app.models import Attempt, LectureProgress, User
from app.services.ai import configured_provider

GOOD_ANSWER = (
    "Subject: Updated shipping date for your shipment\n\n"
    "Dear Buyer,\n\n"
    "I apologize for missing the November 23 shipping date and for the disruption "
    "this has caused. The carrier had to rearrange its vessel capacity. "
    "Our latest shipping schedule confirms November 28 as the new shipping date.\n\n"
    "I will share the shipping documents once the shipment is dispatched. "
    "Thank you for your patience. Please contact me if you need any further details.\n\n"
    "Best regards,\nTrade Operations"
)
WRONG_ANSWER = (
    "Dear Buyer,\n\nThere has been no delay. We guarantee shipment on November 25. "
    "The carrier did not change its vessel capacity, and no written update is needed.\n\n"
    "Regards,\nTrade Operations"
)
DRAFT_REQUEST = (
    "무역 사무 담당자로서 납기 지연에 항의한 바이어에게 보낼 영문 비즈니스 이메일을 작성해 주세요. "
    "제공 자료에서 기존 선적일은 11월 23일, 사유는 선사의 선복 조정, 25일은 미확정, 28일은 확정입니다. "
    "계약서에는 지연 사유와 확정 일정을 서면으로 통지하도록 되어 있습니다. "
    "제목, 사과, 원인, 확정일, 다음 안내를 넣고 미확정 날짜와 보상은 약속하지 마세요. "
    "110단어 이내의 초안 한 개만 반환하세요."
)
VERIFY_AND_IMPROVE = (
    "초안을 원자료와 항목별로 대조했습니다. 바이어 원문의 기존 날짜는 11월 23일입니다. "
    "원본 선적 일정표의 25일은 미확정이며 28일 행에만 확정 표시가 있습니다. "
    "일정표의 비고에 지연 사유가 선사의 선복 조정으로 적혀 있고 계약서에는 사유와 확정 일정을 "
    "서면으로 통지하라고 적혀 있어 두 정보가 초안과 일치합니다. "
    "개선하려면 첫 문장에 지연에 대한 사과를 두고, 확정 날짜를 두 번째 단락에서 찾게 하지 말고 "
    "사유 바로 뒤에 명확히 제시해 주세요. 바이어가 후속 조치를 알도록 선적 후 서류 공유 안내를 추가하고 "
    "새 약속이나 미확정 날짜는 제외해 주세요. 수정된 영어 이메일 본문만 작성하세요."
)
CASES = (
    ("both_pass", "두 영역 충족", "verified", GOOD_ANSWER, (True, True, True)),
    ("result_only", "실무 과제 해결만 충족", "none", GOOD_ANSWER, (True, False, False)),
    ("ai_only", "AI 활용만 충족", "verified", WRONG_ANSWER, (False, True, False)),
    (
        "without_verification",
        "AI 대화 후 원자료를 검증하지 않음",
        "unverified",
        GOOD_ANSWER,
        (True, False, False),
    ),
)


class FixtureClock:
    def __init__(self):
        self.value = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


def read_response(response, expected: int):
    if response.status_code != expected:
        raise RuntimeError(
            f"{response.request.url.path}: HTTP {response.status_code}: {response.text}"
        )
    return response.json()


def unlock(client, headers, lecture: dict, clock: FixtureClock):
    duration = lecture["duration_sec"]
    threshold = (duration * 4 + 4) // 5
    for start in range(0, threshold, 10):
        clock.advance(10)
        end = min(start + 10, threshold)
        progress = read_response(
            client.post(
                f"/api/v1/lectures/{lecture['id']}/progress",
                headers=headers,
                json={"watched_ranges": [[start, end]], "position_sec": end},
            ),
            200,
        )
    if not progress["completed"]:
        raise RuntimeError("Fixture progress did not unlock the task.")


def record_chat(client, path, headers, text):
    return read_response(
        client.post(path + "/messages", headers=headers, json={"content": text}), 201
    )


def record_revision(client, path, headers, content):
    read_response(
        client.put(path + "/answer", headers=headers, json={"content": content}), 200
    )


def score_case(client, headers, task_id: int, case: tuple) -> dict:
    case_id, label, history, answer, expected = case
    attempt = read_response(
        client.post("/api/v1/attempts", headers=headers, json={"task_id": task_id}),
        201,
    )
    path = f"/api/v1/attempts/{attempt['id']}"
    started = monotonic()
    draft = (
        record_chat(client, path, headers, DRAFT_REQUEST) if history != "none" else None
    )
    if history == "verified":
        record_revision(client, path, headers, draft["assistant_message"]["content"])
        revised = record_chat(client, path, headers, VERIFY_AND_IMPROVE)
        record_revision(client, path, headers, revised["assistant_message"]["content"])
    if history == "unverified":
        record_revision(client, path, headers, draft["assistant_message"]["content"])

    read_response(
        client.post(path + "/submit", headers=headers, json={"answer": answer}), 202
    )
    result = read_response(client.get(path, headers=headers), 200)
    dims = {row["code"]: row for row in result["dimensions"] or []}
    actual = (
        dims.get("TASK_SOLUTION", {}).get("passed"),
        dims.get("AI_USE", {}).get("passed"),
        result["passed"],
    )
    verify_score = next(
        (
            row["score"]
            for row in result["scores"] or []
            if row["criterion"] == "VERIFY"
        ),
        None,
    )
    matches = actual == expected and (history == "verified" or verify_score == 0)
    return {
        "case": case_id,
        "label": label,
        "expected": expected,
        "actual": actual,
        "matches": matches,
        "status": result["status"],
        "total_score": result["total_score"],
        "dimensions": result["dimensions"],
        "criteria": result["scores"],
        "required_checks": result["required_checks"],
        "summary_feedback": result["summary_feedback"],
        "duration_seconds": round(monotonic() - started, 2),
    }


def validate(repetitions: int) -> dict:
    provider = configured_provider()
    if not provider.base_url or not provider.model:
        raise RuntimeError(
            "Configure AI_BASE_URL and AI_MODEL before validating a real model."
        )
    url = database_url("TEST_DATABASE_URL")
    if not url.database.endswith("_test"):
        raise RuntimeError(
            "A dedicated _test database is required for the synthetic accounts."
        )
    engine = make_engine(url)
    clock = FixtureClock()
    app = create_app(
        engine,
        clock=clock,
        jwt_secret=token_urlsafe(40),
        provider=provider,
        recover_inflight=False,
    )
    user_id = None
    login_id = "grade_" + uuid4().hex[:16]
    rows = []
    try:
        with TestClient(app) as client:
            jobs = read_response(client.get("/api/v1/jobs"), 200)
            job_id = next(job["id"] for job in jobs if job["name"] == "무역 사무")
            credentials = {"login_id": login_id, "password": token_urlsafe(18)}
            user_id = read_response(
                client.post(
                    "/api/v1/auth/signup",
                    json={**credentials, "nickname": "채점 검증", "job_id": job_id},
                ),
                201,
            )["id"]
            token = read_response(
                client.post("/api/v1/auth/login", json=credentials), 200
            )["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            tasks = read_response(client.get("/api/v1/tasks", headers=headers), 200)
            task = next(
                row
                for row in tasks
                if row["title"] == "바이어 납기 지연 항의 메일 회신"
            )
            lessons = read_response(
                client.get("/api/v1/lectures", headers=headers), 200
            )["lectures"]
            lecture = next(
                row for row in lessons if row["id"] == task["required_lecture"]["id"]
            )
            unlock(client, headers, lecture, clock)
            for repetition in range(1, repetitions + 1):
                for case in CASES:
                    row = score_case(client, headers, task["id"], case)
                    rows.append({"run": repetition, **row})
                    print(
                        f"{repetition} {row['case']}: {row['status']} "
                        f"{row['total_score']} {row['actual']} ({row['duration_seconds']}s)",
                        flush=True,
                    )
    finally:
        if user_id is not None:
            with Session(engine) as session, session.begin():
                session.execute(delete(Attempt).where(Attempt.user_id == user_id))
                session.execute(
                    delete(LectureProgress).where(LectureProgress.user_id == user_id)
                )
                session.execute(
                    delete(User).where(User.id == user_id, User.login_id == login_id)
                )
        engine.dispose()
    return {"model": provider.model, "rubric": "DUAL_V1", "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()
    outcome = validate(args.repetitions)
    args.output.write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not all(row["matches"] for row in outcome["cases"]):
        raise SystemExit(
            "One or more real-model cases did not match the expected judgment."
        )


if __name__ == "__main__":
    main()
