def bearer(response):
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def report(client, headers, lecture, ranges, position):
    return client.post(
        f"/api/v1/lectures/{lecture}/progress",
        headers=headers,
        json={"watched_ranges": ranges, "position_sec": position},
    )


def watch_to(client, headers, clock, lecture, seconds):
    for start in range(0, seconds, 10):
        clock.advance(seconds=10)
        end = min(start + 10, seconds)
        response = report(client, headers, lecture, [[start, end]], end)
        assert response.status_code == 200, response.text
    return response.json()


class FixtureProvider:
    def __init__(self, points=(40, 14, 12, 14), required=True, invalid=False):
        self.points = points
        self.required = required
        self.invalid = invalid
        self.inputs = []

    def chat(self, messages):
        return "확정 선적일을 원자료로 확인하세요."

    def grade(self, context):
        self.inputs.append(context)
        return {
            "scores": [
                {
                    "criterion": key,
                    "score": value,
                    "feedback": "평가 근거",
                    "evidence_message_id": 2**60
                    if self.invalid
                    else (
                        next(
                            (
                                message["id"] for message in reversed(context["messages"])
                                if message.get("role") == "USER"
                            ),
                            None,
                        )
                        if key == "VERIFY" and value
                        else None
                    ),
                }
                for key, value in zip(
                    ("RESULT", "PROMPT", "VERIFY", "IMPROVE"), self.points
                )
            ],
            "required_checks": [
                {
                    "check_id": "SHIPPING_DATE",
                    "met": self.required,
                    "feedback": "확정일 근거",
                }
            ],
            "summary_feedback": "다음에도 최신 선적 일정표를 확인하세요.",
        }


def submit_with_evidence(client, headers, task_id, answer="November 28 is confirmed."):
    start = client.post("/api/v1/attempts", headers=headers, json={"task_id": task_id})
    assert start.status_code == 201, start.text
    path = f"/api/v1/attempts/{start.json()['id']}"
    for message in (
        "자료를 참고해 영문 회신을 작성해 주세요.",
        "선적 일정표의 확정일 11월 28일과 답변을 대조했습니다.",
    ):
        assert client.post(
            path + "/messages", headers=headers, json={"content": message}
        ).status_code == 201
    accepted = client.post(path + "/submit", headers=headers, json={"answer": answer})
    assert accepted.status_code == 202, accepted.text
    return path
