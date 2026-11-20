# 백엔드 MVP 개발 계획

> 실행할 때는 Superpowers의 `executing-plans` 또는 `subagent-driven-development`를 적용합니다. 각 작업은 실패하는 테스트 작성, 최소 구현, 테스트 통과, 변경 검토 순서로 진행합니다.

**목표**

API 클라이언트만으로 무역 사무 강의 시청 기록 제출부터 과제 응시, AI 채점, 결과 조회까지 실행할 수 있게 합니다.

**구조**

FastAPI가 인증부터 응시 결과까지의 REST API와 맵 상태용 WebSocket을 제공합니다. MySQL 8이 수강·응시·채점 상태를 관리합니다. AI 대화와 채점은 교체 가능한 두 함수로 연결하고, 테스트에서는 고정 응답을 주입합니다.

**구현 기본값**

아래 경로는 백엔드 저장소 루트를 기준으로 합니다. DB 모델과 마이그레이션에는 SQLAlchemy와 Alembic, 검증에는 pytest를 사용합니다. Phaser 프론트엔드 팀에는 공개 HTTP·WebSocket 계약과 재현 가능한 시드 데이터를 전달합니다.

**기준 명세**

[ERD](1_ERD.md) · [API 명세서](2_API명세서.md)

## 공통 규칙

- REST 기본 경로는 `/api/v1`입니다. 응답 필드는 `snake_case`이며, 시간은 DB에는 UTC로 저장하고 API에서는 `Z`로 직렬화합니다. MySQL 문자셋은 `utf8mb4`를 사용합니다.
- 수강 완료는 중복을 제외한 시청 구간이 80% 이상일 때입니다. 과제 안내와 응시 생성에서 서버가 해당 과제의 필수 강의를 확인합니다.
- 사용자당 `IN_PROGRESS` 응시 한 건을 허용합니다. 새 응시에 채점 기준을 복사하고, 이후 조회·자동 제출·재시도는 같은 스냅샷을 사용합니다.
- `DUAL_V1` 배점은 결과물 50, 질문 설계 15, 답변 검증 20, 개선 과정 15입니다. 결과물 40점 이상과 필수 요건 충족, AI 활용 합계 40점 이상과 검증 12점 이상을 모두 만족해야 통과합니다.
- 맵의 접속 상태와 채점 작업이 프로세스 메모리에 있으므로 MVP 실행 환경은 Uvicorn 워커 1개를 기준으로 검증합니다.
- 트랜잭션, 생성 컬럼, 외래 키 동작은 MySQL 8에서 검증합니다. 외부 AI 없이 실행하는 통합 테스트에는 테스트 DB, 제어 가능한 서버 시계, 고정 AI 응답을 사용합니다.

## 모듈과 구현 순서

| 단계 | 구현 단위 | 제안 파일 | 검증 파일 |
|---|---|---|---|
| 1 | 환경·스키마·시드 | `app/main.py`, `app/db.py`, `app/clock.py`, `app/models/`, `migrations/`, `scripts/seed.py` | `tests/test_database.py` |
| 2 | 인증·직무·회원 | `app/api/auth.py`, `app/api/users.py`, `app/api/jobs.py`, `app/services/auth.py` | `tests/test_auth.py` |
| 3 | 수강·과제 해금 | `app/api/lectures.py`, `app/api/tasks.py`, `app/services/progress.py` | `tests/test_progress.py` |
| 4 | 응시·과제 수행·제출 | `app/api/attempts.py`, `app/services/attempts.py`, `app/services/ai.py` | `tests/test_attempts.py` |
| 5 | AI 채점·결과·레벨 | `app/services/grading.py`, `app/services/results.py`, `app/api/attempts.py` | `tests/test_grading.py`, `tests/test_results.py` |
| 6 | 맵 WebSocket | `app/api/map_ws.py`, `app/services/presence.py` | `tests/test_map_ws.py` |
| 7 | 통합 검증·인수인계 | `README.md`, `tests/test_mvp_flow.py`, `tests/fixtures/` | `python -m pytest -q` |

## 작업별 구현 체크리스트

### 1. 실행 환경과 데이터베이스

- [x] FastAPI 진입점, 환경 설정, DB 세션, UTC 서버 시계를 구성합니다. `utc_now()`를 수강·응시·채점에서 공통으로 사용하고 테스트에서 교체할 수 있게 합니다.
- [x] ERD의 PK·FK·UNIQUE·삭제 연쇄 제약을 마이그레이션으로 적용합니다. `attempt.in_progress_uid`의 생성 컬럼 및 유니크 인덱스, `lecture_progress`와 `attempt_score`의 복합 키를 포함합니다.
- [x] 무역 사무 직무, 강의 한 개, 바이어 납기 지연 회신 과제, 자료 본문, 필수 요건이 포함된 `DUAL_V1` 기준으로 시드를 만듭니다. 과제와 강의의 직무 일치도 확인합니다.
- [x] `python -m alembic upgrade head`로 빈 MySQL에 스키마를 올립니다. `python -m pytest -q tests/test_database.py`에서 동일 사용자 활성 응시 중복을 DB가 거부하고, 응시 삭제 시 자식 데이터가 함께 삭제되는지 확인합니다. 기존 데이터가 이관되는 환경이면 `LEGACY_TOTAL_V1`로 스냅샷을 채우고 과거 점수와 통과 결과를 보존하는 마이그레이션도 검증합니다.

### 2. 인증·직무·회원

- [x] `GET /jobs`, `POST /auth/signup`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /users/me`, `PATCH /users/me`를 구현합니다. API 명세의 공통 에러 응답과 오류 코드를 적용합니다.
- [x] bcrypt 비밀번호 해시, HS256 액세스 토큰 1시간, 무작위 리프레시 토큰 14일을 적용합니다. DB에는 리프레시 토큰 해시를 저장합니다. 쿠키는 `HttpOnly`, `SameSite=Lax`, `Path=/api/v1/auth`를 적용하고 배포에서 `Secure`를 적용합니다.
- [x] 재발급은 이전 토큰 폐기와 신규 토큰 생성을 한 트랜잭션에서 처리합니다. 인증 로직은 이후 REST와 WebSocket이 공유할 수 있도록 `verify_access_token(token: str) -> int`로 분리합니다.
- [x] `python -m pytest -q tests/test_auth.py`에서 중복 회원가입, 잘못된 로그인, 만료 토큰, 재발급 후 이전 토큰 재사용, 동시 재발급, 명세 범위의 멀티바이트 비밀번호 가입·로그인, 로그아웃 후 쿠키 제거와 리프레시 토큰 폐기를 확인합니다.

### 3. 수강 진도와 필수 과제 해금

- [x] `GET /lectures`, `GET /lectures/{id}`, `POST /lectures/{id}/progress`, `GET /tasks`, `GET /tasks/{id}`를 구현합니다. 강의 상세에 연결 과제 `task_id`를, 잠긴 과제 목록에는 `available: false`와 필수 강의를 반환합니다. 과제 상세에는 공개 평가 기준과 자료 제목을 반환합니다.
- [x] `merge_watched_ranges(existing, incoming, duration_sec)`로 겹치는 구간을 합칩니다. 첫 보고는 15초까지, 이후에는 마지막 보고 후 경과 시간 + 5초까지 새 보고를 인정합니다. 진도 행을 잠근 상태에서 합산하고, 반올림 전 실제 비율로 최초 80% 도달 여부를 결정합니다.
- [x] 공통 수강 확인 로직을 과제 상세와 단계 4의 응시 생성에서 사용합니다. 다른 직무의 과제·강의는 각 리소스의 404를 반환합니다.
- [x] `python -m pytest -q tests/test_progress.py`에서 600초 강의의 479초 시청은 잠김, 480초는 해금, 480초 재조회도 해금인지 검증합니다. 중복 구간, 건너뛴 구간, 동시 보고, 다른 강의 수강에 의한 우회도 검사합니다.

### 4. 응시 시작·저장·이어 풀기·제출

- [x] `POST /attempts`에서 필수 강의 수강 여부를 서버가 다시 확인합니다. 미충족이면 `403 LECTURE_NOT_COMPLETED`를 반환합니다. 성공 시 사용자 행 잠금과 DB 유니크 제약으로 동시 시작을 차단하고, 채점 기준을 복사한 뒤 `started_at`과 `deadline_at`을 저장합니다.
- [x] `GET /attempts/current`와 `GET /attempts/{id}/messages`로 진행 중 응시의 저장 답안·대화·남은 시간·공개 평가 기준을 반환합니다. 진행 중 응시가 없으면 확인할 결과를 찾고, 둘 다 없으면 204를 반환합니다. `DELETE /attempts/{id}`는 소유자의 진행 중 응시에 적용합니다.
- [x] `PUT /attempts/{id}/answer`는 직전 내용과 달라질 때 수정 이력을 저장합니다. `POST /attempts/{id}/events`는 명세의 세 이벤트와 최대 1KB를 검증합니다. `POST /attempts/{id}/messages`는 `chat(messages) -> str` 어댑터를 호출합니다. 성공한 질문과 응답을 함께 저장하고, 실패 시 `502 AI_UNAVAILABLE`을 반환합니다. AI 호출이 완료된 뒤에도 응시 상태와 마감을 확인합니다.
- [x] `POST /attempts/{id}/submit`과 만료 자동 제출이 같은 조건부 상태 전이(`IN_PROGRESS` → `GRADING`)를 사용하도록 구현합니다. 수동 제출은 마감 + 1분까지 받으며, 그 이후에는 명세의 네 진입점에서 마지막 저장 답안을 자동 제출합니다. 채점 작업은 상태 전이가 실제로 성공하고 커밋된 응시에 대해 한 번 예약합니다.
- [x] `auto_submitted`는 상태 전이에서 비교와 저장에 함께 사용한 초 단위 UTC `submitted_at`과 `deadline_at + 1분`을 비교해 계산합니다. 수동 제출은 경계까지, 자동 제출은 그 경계를 지난 경우로 구분합니다.
- [x] `python -m pytest -q tests/test_attempts.py`에서 80% 미달 응시 요청, 동시 시작, 다른 사용자 응시 접근, 마감 후 쓰기, 제출 경계, 자동·수동 제출 경합, 마지막 답안 복원, AI 실패 시 대화 쌍 미저장을 확인합니다.

### 5. AI 채점·결과·레벨

- [x] `grade(context)`가 응시 스냅샷·최종 답안·자료·대화·수정 이력·행동 기록을 받아 항목별 점수와 근거, 필수 요건별 판정, 총평을 반환하도록 구현합니다. 시스템 지침과 평가 데이터를 구분합니다. 고정 응답으로 먼저 검증하고 실제 AI 모델은 같은 인터페이스에 연결합니다.
- [x] `validate_grade_output(snapshot, output, evidence)`에서 항목별 배점, 중복·누락, 필수 요건 ID, 동일 응시 근거를 확인합니다. 양수인 `VERIFY`에는 검증 근거가 필요합니다. 응답이 잘못되면 한 번 다시 호출하고, 계속 잘못되면 `GRADING_FAILED`로 전환합니다.
- [x] `evaluate_pass(snapshot, scores, required_checks)`로 서버가 총점과 두 영역의 충족 여부를 결정합니다. 기존 기준은 저장된 버전에 따라 계산합니다. 점수·필수 요건 결과·레벨을 한 트랜잭션으로 저장하며 `GRADING`을 `GRADED`로 바꿉니다. 같은 과제 통과는 레벨 계산에서 한 번 셉니다.
- [x] `GET /attempts/{id}`, `GET /attempts`, `POST /attempts/{id}/regrade`를 연결합니다. 결과는 `pass_requirements`, `dimensions`, `required_checks`와 근거를 반환합니다. 목록은 최신순, `task_id` 필터, `page`·`size` 페이지네이션을 적용합니다. `result_seen_at`의 조건부 갱신으로 최초 채점 완료 조회 한 건에만 `first_view: true`를 반환합니다. 재시도와 서버 재시작 시 명세의 상태 전이를 따릅니다.
- [x] `python -m pytest -q tests/test_grading.py tests/test_results.py`로 최소 점수에서 통과, 총점 85·검증 11점에서 미통과, 총점 100·필수 요건 하나 누락에서 미통과를 확인합니다. 근거 ID 오류, 스냅샷 변경 후 재시도, 동시 채점의 레벨 중복 상승, 저장 실패 시 점수·레벨 롤백, 채점 중 서버 재시작도 검증합니다.
- [x] 실제 채점기에 통과 답안, 결과물만 좋은 답안, 과정만 좋은 답안과 원자료 검증 없이 작성한 답안을 넣습니다. 각 응답이 해당 기준과 기록에 근거했는지 확인한 뒤 프론트엔드 통합용으로 사용합니다.

### 6. 맵 WebSocket

- [x] `/api/v1/ws/map`의 첫 인증 메시지, 5초 제한, `4401`·`4409`, 초기 스냅샷과 참여·이동·이탈 이벤트를 구현합니다. 테스트용 경계 값을 설정에 두고 Phaser 맵의 경계 값은 프론트엔드와 공유합니다. Phaser 월드의 `x/y`를 웹소켓 송수신에 그대로 사용합니다.
- [x] `x/y/yaw` 좌표·회전과 `moving`을 검증합니다. 좌표를 경계로 제한하고, 초당 10회를 넘는 이동을 버립니다. 채점 커밋 후 레벨이 오르면 `player_updated`를 보냅니다.
- [x] `python -m pytest -q tests/test_map_ws.py`에서 테스트 클라이언트 두 개로 인증·좌표 공유·중복 접속·종료 코드·레벨 변경을 확인합니다.

### 7. 인수 조건과 전달물

- [x] `python -m pytest -q tests/test_mvp_flow.py`로 가입·로그인 → 479초 시험 시작 거절 → 480초 해금 → 응시·답안 저장 → 이어 풀기 → 제출 → 두 영역 결과 조회를 검증합니다.
- [x] 실패 경로는 마감 후 자동 제출, 채점 실패 후 재시도, 결과 조회 중복을 포함합니다. MySQL 8에서 `python -m pytest -q` 전체 검증을 실행합니다.
- [x] 인증·소유권, 응시와 제출의 경쟁 조건, 채점 트랜잭션을 독립 리뷰에 올리고 발견한 문제를 해결한 뒤 최종 검증을 다시 실행합니다.
- [x] 실행 방법, 환경 변수 이름, 마이그레이션·시드 명령, API 응답 예시, WebSocket 테스트 방법을 README에 남깁니다. 프론트엔드에 테스트 계정 생성 흐름과 강의·과제 시드, FastAPI의 OpenAPI 계약을 전달합니다.

## 리뷰 집중 항목

| 확인할 실패 조건 | 담당 작업과 기대 결과 |
|---|---|
| 같은 세션 토큰으로 재발급 요청 두 건이 겹침 | 단계 2. 하나만 성공하고 폐기된 토큰 재사용은 실패 |
| 79.8%가 화면 값에서 0.80으로 반올림되거나 다른 강의만 완료함 | 단계 3·4. 실제 시청 비율과 연결된 강의로 응시를 차단 |
| 마감 이후 AI 응답이 도착하거나 수동 제출과 자동 제출이 동시에 처리됨 | 단계 4. 마감 후 대화 쌍은 저장되지 않고 최종 답안·채점 작업은 한 번만 확정 |
| 모델이 없는 근거 또는 다른 응시의 근거를 반환함 | 단계 5. 응답을 재요청하고 반복 실패 시 점수 없이 `GRADING_FAILED` |
| 같은 과제를 이미 통과했거나 두 결과가 동시에 저장됨 | 단계 5. 통과한 서로 다른 과제 수로 레벨 계산. 최초 조회 상태와 레벨 변경 이벤트가 중복되지 않음 |

1~7단계를 구현하고 실제 로컬 Qwen 모델로 기준 답안을 검증했습니다. 네 가지 합성 사례를 각각 두 번 실행해 8건 모두 기대 판정과 일치했습니다. [실제 AI 채점 검증 결과](실제_AI_채점_검증.md)
