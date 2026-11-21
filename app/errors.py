from fastapi import Request
from fastapi.responses import JSONResponse

ERRORS = {
    "INVALID_CREDENTIALS": (401, "아이디 또는 비밀번호가 틀렸어요."),
    "TOKEN_EXPIRED": (401, "액세스 토큰이 만료됐어요."),
    "INVALID_TOKEN": (401, "유효한 액세스 토큰이 필요해요."),
    "INVALID_REFRESH_TOKEN": (401, "다시 로그인해 주세요."),
    "LOGIN_ID_TAKEN": (409, "이미 사용 중인 아이디예요."),
    "INVALID_JOB": (400, "선택한 직무를 찾을 수 없어요."),
    "LECTURE_NOT_FOUND": (404, "강의를 찾을 수 없어요."),
    "TASK_NOT_FOUND": (404, "과제를 찾을 수 없어요."),
    "ATTEMPT_NOT_FOUND": (404, "응시 기록을 찾을 수 없어요."),
    "LECTURE_NOT_COMPLETED": (403, "이 과제는 필수 강의를 먼저 들어야 볼 수 있어요."),
    "ATTEMPT_NOT_IN_PROGRESS": (409, "진행 중인 응시에서만 할 수 있어요."),
    "ATTEMPT_ALREADY_IN_PROGRESS": (409, "진행 중인 응시가 있어요."),
    "ATTEMPT_TIME_OVER": (400, "제한 시간이 지났어요."),
    "ATTEMPT_NOT_REGRADABLE": (409, "다시 채점할 수 없는 상태예요."),
    "CHAT_IN_PROGRESS": (409, "앞선 AI 답변을 받은 뒤 다시 보내 주세요."),
    "IDEMPOTENCY_CONFLICT": (409, "이미 다른 질문에 사용한 요청 식별자예요."),
    "ANSWER_REVISION_CONFLICT": (409, "더 최근에 저장된 답안이 있어요."),
    "AI_BUSY": (429, "AI 요청이 많아요. 잠시 뒤 다시 시도해 주세요."),
    "ATTEMPT_INPUT_LIMIT": (429, "이 응시의 기록 한도에 도달했어요."),
    "RATE_LIMITED": (429, "잠시 후 다시 시도해 주세요."),
    "AI_UNAVAILABLE": (502, "AI 연결에 실패했어요. 다시 시도해 주세요."),
}


class ApiError(Exception):
    def __init__(self, code: str, *, headers: dict[str, str] | None = None):
        self.code = code
        self.headers = headers
        super().__init__(code)


async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
    status, message = ERRORS[error.code]
    return JSONResponse(
        {"error": {"code": error.code, "message": message}},
        status_code=status,
        headers=error.headers,
    )
