import json

import pytest

from app.services import ai


def test_local_endpoint_does_not_receive_an_openai_key(monkeypatch):
    values = {
        "AI_BASE_URL": "http://127.0.0.1:8091/v1",
        "AI_MODEL": "qwen3-4b-instruct-2507",
        "OPENAI_API_KEY": "a-test-key-for-openai",
    }
    monkeypatch.setattr(ai, "setting", values.get)
    assert ai.configured_provider().key is None
    values["AI_API_KEY"] = "explicit-local-key"
    assert ai.configured_provider().key == "explicit-local-key"


def test_default_openai_provider_accepts_the_conventional_key(monkeypatch):
    values = {"AI_MODEL": "fixture-model", "OPENAI_API_KEY": "a-test-key-for-openai"}
    monkeypatch.setattr(ai, "setting", values.get)
    provider = ai.configured_provider()
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.key == "a-test-key-for-openai"


def test_grading_keeps_the_submitted_answer_and_ai_process_separate(monkeypatch):
    calls = []

    def fake_completion(self, messages, **options):
        schema = options["response_format"]["json_schema"]
        calls.append((schema, json.loads(messages[1]["content"]), messages[0]["content"]))
        if schema["name"] == "task_solution":
            return json.dumps(
                {
                    "score": 20,
                    "feedback": "잘못된 날짜",
                    "required_checks": [],
                    "summary_feedback": "다음에 날짜를 비교하세요.",
                }
            )
        item = {
            "score": 14,
            "feedback": "과정 근거",
            "evidence_message_id": 5,
            "evidence_revision_id": None,
        }
        return json.dumps(
            {
                "PROMPT": item,
                "IMPROVE": item,
                "VERIFY": {
                    **item,
                    "evidence_message_id": None,
                    "evidence_revision_id": 7,
                },
            }
        )

    monkeypatch.setattr(ai.ChatCompletionsProvider, "complete", fake_completion)
    provider = ai.ChatCompletionsProvider(
        "http://127.0.0.1:8091/v1", "fixture-model", None
    )
    raw = provider.grade(
        {
            "rubric": {
                "version": "DUAL_V1",
                "task_solution_min_score": 32,
                "ai_use_min_score": 24,
                "verify_min_score": 8,
                "criterion_max_scores": {
                    "RESULT": 40,
                    "PROMPT": 12,
                    "VERIFY": 16,
                    "IMPROVE": 12,
                },
            },
            "task": {},
            "materials": [],
            "answer": "Wrong final answer",
            "messages": [
                {
                    "id": 4,
                    "role": "ASSISTANT",
                    "content": "Draft",
                    "created_at": "2026-11-20T03:00:00Z",
                },
                {
                    "id": 5,
                    "role": "USER",
                    "content": "자료와 대조했습니다.",
                    "created_at": "2026-11-20T03:00:00Z",
                },
            ],
            "revisions": [
                {
                    "id": 7,
                    "content": "Correct draft",
                    "created_at": "2026-11-20T03:00:00Z",
                    "after_message_id": 4,
                }
            ],
            "events": [{"type": "FOCUS", "payload": {"noise": "ignored"}}],
        }
    )
    result_request, process_request = calls
    assert result_request[1]["submitted_answer"] == "Wrong final answer"
    assert "messages" not in result_request[1] and "revisions" not in result_request[1]
    assert process_request[1]["revisions"] == [{"id": 7, "content": "Correct draft"}]
    assert process_request[1]["events"] == {"FOCUS": 1}
    assert (
        "answer" not in process_request[1]
        and "submitted_answer" not in process_request[1]
    )
    assert all(call[0]["schema"]["additionalProperties"] is False for call in calls)
    assert (
        process_request[0]["schema"]["$defs"]["ProcessScore"]["properties"]["feedback"][
            "maxLength"
        ]
        == 80
    )
    assert [row["score"] for row in raw["scores"]] == [20, 14, 14, 14]

    evidence = process_request[0]["schema"]["$defs"]["ProcessScore"]["properties"]
    assert evidence["evidence_message_id"]["anyOf"][1]["enum"] == [5]
    assert evidence["evidence_revision_id"]["anyOf"][1]["enum"] == [7]
    assert "RESULT 최대 40점" in result_request[2]
    assert "통과 기준은 32점" in result_request[2]
    assert "PROMPT 최대 12점" in process_request[2]
    assert "VERIFY 최대 16점" in process_request[2]
    assert "IMPROVE 최대 12점" in process_request[2]
    result_score = result_request[0]["schema"]["properties"]["score"]
    assert (result_score["minimum"], result_score["maximum"]) == (0, 40)
    for criterion, limit in (("PROMPT", 12), ("VERIFY", 16), ("IMPROVE", 12)):
        score_ref = process_request[0]["schema"]["properties"][criterion]["$ref"]
        score_def = process_request[0]["schema"]["$defs"][score_ref.rsplit("/", 1)[-1]]
        assert score_def["properties"]["score"]["maximum"] == limit


def test_missing_ai_history_is_scored_as_zero_without_guessing_evidence(monkeypatch):
    calls = []

    def fake_completion(self, messages, **options):
        calls.append(options["response_format"]["json_schema"]["name"])
        return json.dumps(
            {
                "score": 45,
                "feedback": "확정일 일치",
                "required_checks": [],
                "summary_feedback": "최신 일정표를 사용하세요.",
            }
        )

    monkeypatch.setattr(ai.ChatCompletionsProvider, "complete", fake_completion)
    provider = ai.ChatCompletionsProvider(
        "http://127.0.0.1:8091/v1", "fixture-model", None
    )
    graded = provider.grade(
        {
            "rubric": {"version": "DUAL_V1", "criterion_max_scores": {}},
            "task": {},
            "materials": [],
            "answer": "Good answer",
            "messages": [],
            "revisions": [
                {
                    "id": 9,
                    "content": "AI 대화 없이 저장한 초안",
                    "created_at": "2026-11-20T03:00:00Z",
                    "after_message_id": None,
                }
            ],
            "events": [],
        }
    )
    assert calls == ["task_solution"]
    assert [row["score"] for row in graded["scores"]] == [45, 0, 0, 0]
    assert all(
        row.get("evidence_message_id") is None
        and row.get("evidence_revision_id") is None
        for row in graded["scores"][1:]
    )


def test_saved_assistant_text_is_not_a_student_verification(monkeypatch):
    def fake_completion(self, messages, **options):
        name = options["response_format"]["json_schema"]["name"]
        if name == "task_solution":
            return json.dumps(
                {
                    "score": 45,
                    "feedback": "확정일 일치",
                    "required_checks": [],
                    "summary_feedback": "최신 자료를 확인하세요.",
                }
            )
        return json.dumps(
            {
                key: {
                    "score": 15,
                    "feedback": "복사된 초안",
                    "evidence_message_id": 2,
                    "evidence_revision_id": 3,
                }
                for key in ("PROMPT", "VERIFY", "IMPROVE")
            }
        )

    monkeypatch.setattr(ai.ChatCompletionsProvider, "complete", fake_completion)
    provider = ai.ChatCompletionsProvider(
        "http://127.0.0.1:8091/v1", "fixture-model", None
    )
    graded = provider.grade(
        {
            "rubric": {"version": "DUAL_V1", "criterion_max_scores": {}},
            "task": {},
            "materials": [],
            "answer": "Actual answer",
            "messages": [
                {"id": 1, "role": "USER", "content": "초안을 작성해 줘."},
                {"id": 2, "role": "ASSISTANT", "content": "AI가 작성한 회신"},
            ],
            "revisions": [{"id": 3, "content": "AI가 작성한 회신"}],
            "events": [],
        }
    )
    verify = next(row for row in graded["scores"] if row["criterion"] == "VERIFY")
    assert verify["score"] == 0
    assert (
        verify["evidence_message_id"] is None and verify["evidence_revision_id"] is None
    )


def test_verification_evidence_requires_learner_action_after_ai_response():
    context = {
        "messages": [
            {
                "id": 1,
                "role": "USER",
                "content": "초안을 써 주세요.",
                "created_at": "2026-11-20T03:00:00Z",
            },
            {
                "id": 2,
                "role": "ASSISTANT",
                "content": " AI   초안\n",
                "created_at": "2026-11-20T03:00:01Z",
            },
            {
                "id": 3,
                "role": "USER",
                "content": "AI 초안",
                "created_at": "2026-11-20T03:00:01Z",
            },
            {
                "id": 4,
                "role": "USER",
                "content": "일정표와 대조해 28일로 고쳐 주세요.",
                "created_at": "2026-11-20T03:00:01Z",
            },
        ],
        "revisions": [
            {
                "id": 10,
                "content": "AI 전 초안",
                "created_at": "2026-11-20T03:00:00Z",
                "after_message_id": None,
            },
            {
                "id": 11,
                "content": "AI 초안",
                "created_at": "2026-11-20T03:00:01Z",
                "after_message_id": 2,
            },
            {
                "id": 12,
                "content": "28일 확정으로 수정",
                "created_at": "2026-11-20T03:00:01Z",
                "after_message_id": 2,
            },
        ],
    }

    assert ai.verification_evidence(context) == ([4], [12])
    assert ai.validate_verification_reference(context, 4, None)
    assert ai.validate_verification_reference(context, None, 12)
    assert not ai.validate_verification_reference(context, 3, None)
    assert not ai.validate_verification_reference(context, None, 10)
    assert not ai.validate_verification_reference(context, 3, 12)


def test_legacy_revision_without_order_marker_must_be_strictly_after_ai():
    context = {
        "messages": [
            {
                "id": 2,
                "role": "ASSISTANT",
                "content": "AI 초안",
                "created_at": "2026-11-20T03:00:01Z",
            }
        ],
        "revisions": [
            {
                "id": 10,
                "content": "같은 초의 수정",
                "created_at": "2026-11-20T03:00:01Z",
            },
            {
                "id": 11,
                "content": "다음 초의 수정",
                "created_at": "2026-11-20T03:00:02Z",
            },
        ],
    }

    assert ai.verification_evidence(context) == ([], [11])


def test_provider_rejects_oversized_input_and_response(monkeypatch):
    provider = ai.ChatCompletionsProvider("http://local/v1", "fixture", None)
    with pytest.raises(ai.ProviderError, match="input is too large"):
        provider.complete([{"role": "user", "content": "가" * 100_000}])

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit):
            return b"x" * limit

    monkeypatch.setattr(ai, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ai.ProviderError, match="response is too large"):
        provider.complete([{"role": "user", "content": "small"}])
