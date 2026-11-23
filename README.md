# youthgarten API

FastAPI와 MySQL 8 기반의 AI 교육 플랫폼 백엔드입니다. 필수 강의의 중복 제외 시청 구간이 80%에 도달하면 실무 과제에 응시할 수 있습니다. 채점 결과는 실무 과제 해결과 AI 활용의 점수·충족 여부, 필수 요건 판정, 근거를 제공합니다.

- [API 명세서](docs/2_API명세서.md) · [ERD](docs/1_ERD.md) · [백엔드 MVP 개발 계획](docs/백엔드_MVP_개발계획.md)
- [백엔드 개선 계획](docs/superpowers/plans/2026-11-20-backend-improvement.md)
- [실제 AI 채점 검증](docs/실제_AI_채점_검증.md) · [이전 Qwen 채점 결과 JSON](docs/실제_AI_채점_결과.json)

## 로컬 실행

1. Python 3.14와 `uv`를 준비합니다. MySQL 8에서 `utf8mb4` 개발 스키마와 테스트 스키마를 만들고, 개발 계정에 해당 스키마 권한을 설정합니다. 테스트 스키마 이름은 `_test`로 끝나야 합니다.
2. `.env.example`을 `.env`로 복사한 뒤 `DATABASE_URL`과 `TEST_DATABASE_URL`을 실제 접속 주소에 맞춥니다. `JWT_SECRET`은 `python -c "import secrets; print(secrets.token_hex(32))"`로 생성할 수 있습니다. 로컬 HTTP로 실행할 때는 `SECURE_COOKIES=0`을 설정합니다.
3. 저장소 루트에서 다음 명령을 실행합니다.

```powershell
uv sync --locked --python 3.14
uv run --locked python -m alembic upgrade head
uv run --locked python -m scripts.seed
uv run --locked python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

`requirements.txt`로 설치하는 환경에서는 가상환경을 활성화하고 `pip install -r requirements.txt`를 사용합니다. 마이그레이션을 적용한 뒤 `fastapi dev main.py`로 실행할 수 있습니다.

- [Swagger UI](http://127.0.0.1:8000/docs) · [OpenAPI JSON](http://127.0.0.1:8000/openapi.json) · [DB 준비 상태](http://127.0.0.1:8000/health/ready)
- `scripts.seed`는 실제 납기 지연 강의와 과제를 등록합니다. 개발·테스트에서 모의 강의가 필요하면 `scripts.seed --demo`를 실행합니다.

## 환경 변수

| 이름 | 사용 방법 |
|---|---|
| `DATABASE_URL` · `TEST_DATABASE_URL` | 개발·테스트 DB의 `mysql+pymysql://` DSN |
| `JWT_SECRET` · `SECURE_COOKIES` | 최소 32바이트의 JWT 서명 키. 쿠키는 기본적으로 Secure이며, 로컬 HTTP 개발에서만 `SECURE_COOKIES=0`을 명시합니다. |
| `AI_PROVIDER` | 로컬 데모는 `codex_cli`, 배포는 `chat_completions`(기본값) |
| `AI_BASE_URL` · `AI_MODEL` · `AI_API_KEY` | Chat Completions 호환 기본 URL, JSON Schema 응답을 지원하는 모델, 제공자 키. OpenAI 기본 URL에서는 `OPENAI_API_KEY`도 사용 가능 |
| `CODEX_CLI_PATH` · `CODEX_CLI_MODEL` | Codex 네이티브 실행 파일의 절대 경로와 선택 모델. 경로 미지정 시 PATH를 검색하며 모델 미지정 시 CLI 기본값을 사용 |
| `AI_TIMEOUT_SECONDS` | CLI 실행 또는 HTTP 연결·읽기 제한시간. 기본 45초, 1~120초 |
| `MAP_MIN_X`, `MAP_MAX_X`, `MAP_MIN_Y`, `MAP_MAX_Y`, `MAP_SPAWN_X`, `MAP_SPAWN_Y` | Phaser 픽셀 좌표의 경계와 시작 위치. 기본 경계는 `x=0~1920`, `y=0~1408`이고 시작 위치는 `(960, 880)` |
| `WS_ALLOWED_ORIGINS` | 브라우저 WebSocket 출처를 쉼표로 구분한 목록. Vite 개발은 `http://localhost:5173,http://127.0.0.1:5173` |
| `FORWARDED_ALLOW_IPS` | Uvicorn이 전달 헤더를 신뢰할 프록시 IP. 직접 노출 환경은 기본 루프백 값, 같은 출처 프록시를 사용하는 배포는 실제 프록시 주소를 설정 |

## Phaser 연동

프론트 개발 환경에서 `API_TARGET=http://127.0.0.1:8000`을 설정합니다. Vite는 HTTP와 WebSocket의 `/api` 요청을 전달합니다. `WS_ALLOWED_ORIGINS`에 실제 Vite 주소를 넣습니다. 첫 WebSocket 메시지는 `{"type":"auth","access_token":"..."}`입니다. 이동에는 Phaser 맵의 `x/y` 픽셀 좌표와 `dir`(`up`, `down`, `left`, `right`)을 사용합니다. [WebSocket 계약](docs/2_API명세서.md#8-멀티플레이-websocket)

## 강의·과제 게시

[카탈로그 예시](docs/catalog.example.json)를 복사해 승인된 교육 영상 ID와 실제 재생 길이, 과제 지시, 자료, 채점 기준을 채웁니다. 운영 검수에서 영상 삽입 재생과 길이를 확인합니다. 가져오기는 형식과 데이터 관계를 검증합니다.

```powershell
uv run --locked python -m scripts.publish_catalog catalog.json --validate-only
uv run --locked python -m scripts.publish_catalog catalog.json
```

같은 직무에서 같은 `key`와 `version`으로 같은 파일을 적용하면 기존 항목을 반환합니다. 영상이나 평가 자료를 교체할 때는 같은 키의 새 버전을 게시합니다. 새 목록에는 최신 버전을 표시하고 이전 응시와 수강 기록은 기존 버전에 남깁니다.

[납기 지연 회신 카탈로그 v2](docs/catalog.trade-delay.json)는 YouTube 영상 `c9pBqlOSnx4`(697초)를 사용합니다. 누적 558초부터 시험에 응시할 수 있습니다. 기존 MP4 카탈로그 v1에서 v2를 게시하면 새 목록에는 YouTube 강의가 표시되고 종전 시청·응시 기록은 v1에 남습니다.

`python -m scripts.seed`는 기록이 비어 있고 과제 내용이 일치하는 기존 임시 강의의 ID를 재사용합니다. 기존 임시 강의에 기록이 있으면 그 강의를 이전 버전으로 보존하고 새 버전을 등록합니다. Compose에서 마이그레이션 후 운영 데이터에 적용하려면 `docker compose run --rm migrate python -m scripts.seed`를 실행합니다.

같은 과제의 임시 강의가 있고 수강·응시 기록이 비어 있으면, 최초 카탈로그 게시 시 `--adopt-unstarted-lecture <강의 ID>`로 기존 강의와 과제 ID를 사용합니다. 이 옵션은 기존 과제의 내용이 카탈로그와 일치할 때 적용됩니다.

## 실제 AI 모델

### 로컬 데모에서 Codex CLI 사용

Codex CLI를 설치하고 백엔드를 실행할 OS 계정에서 `codex login`을 완료합니다. `codex login status`로 확인한 뒤 `.env`에 다음 값을 설정합니다. Windows에서는 설치된 네이티브 `codex.exe` 경로를 지정합니다. 이 구성은 Codex CLI 0.164.0에서 검증했습니다.

```dotenv
AI_PROVIDER=codex_cli
CODEX_CLI_PATH=C:/path/to/codex.exe
CODEX_CLI_MODEL=
AI_TIMEOUT_SECONDS=45
```

대화와 채점 요청은 임시 작업 디렉터리에서 읽기 전용 CLI 실행으로 처리합니다. 채점 지시는 CLI의 개발자 메시지로, 답안과 대화 기록은 입력 데이터로 전달합니다. 채점 응답에는 JSON Schema를 사용하고 최종 통과는 서버가 실무 과제 해결과 AI 활용 두 영역의 결과로 결정합니다. 모델을 지정하지 않으면 채점 감사 기록에는 `codex-cli-default`가 저장됩니다. CLI 모드는 루프백에 바인딩한 로컬 데모에서 합성 자료로 사용합니다.

### 배포에서 OpenAI API 연결

실제 학습자 트래픽은 `chat_completions`로 처리합니다. Compose 배포는 이 제공자를 사용하도록 설정되어 있습니다. `OPENAI_API_KEY`와 JSON Schema 응답을 지원하는 `AI_MODEL`을 설정합니다. `AI_BASE_URL`을 비워 두면 OpenAI 기본 URL을 사용합니다.

제공자가 응답하지 않을 때 대화 API는 `AI_UNAVAILABLE`을 반환합니다. MySQL에 제출과 채점 작업을 함께 저장하므로 API를 다시 시작해도 작업을 이어갈 수 있습니다. 맵 상태와 요청 한도는 프로세스 내에서 관리하므로 초기 배포의 Uvicorn 워커는 1개로 실행합니다.

## 배포·운영

- Compose를 사용할 때는 별도 파일에 `MYSQL_APP_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `JWT_SECRET`, 모델 설정을 넣고 `docker compose --env-file <환경 파일> up --build`를 실행합니다. DB 접속 URL의 비밀번호에는 URL 안전 문자를 사용합니다. Compose는 MySQL 8과 마이그레이션을 먼저 실행한 뒤 API를 루프백에 엽니다.
- HTTPS 프록시에서 프론트 정적 파일과 `/api`를 같은 출처로 제공하고 WebSocket Upgrade를 전달합니다. 신뢰할 프록시 주소와 브라우저 출처를 제한합니다. 모델 서버 주소는 API 프로세스나 컨테이너에서 연결할 수 있어야 합니다.
- Vercel 프론트를 사용하면 프로젝트의 `VITE_PUBLIC_BACKEND_ORIGIN`에 공개 백엔드 HTTPS 출처를 설정합니다. Vercel은 브라우저의 `/api/v1/` 요청을 이 출처로 전달하고, Phaser는 같은 백엔드의 `wss://` 주소에 직접 접속합니다. 운영의 `WS_ALLOWED_ORIGINS`에 `https://youthgarten.vercel.app`을 포함하고 `SECURE_COOKIES=1`을 사용합니다. Compose는 출처 미지정 시 이 Vercel 주소를 기본값으로 사용합니다.
- `/health/live`는 프로세스, `/health/ready`는 DB 연결, `/health/grading`은 채점 작업 상태·모델 설정 여부를 확인합니다. 실제 모델 응답 검증은 아래 별도 명령으로 실행합니다.
- `uv run --locked python -m scripts.prune_sessions`로 만료 7일이 지난 토큰의 정리 대상을 확인합니다. 운영 보존 정책에 맞춰 `--apply`를 지정하면 한 번에 기본 1,000행을 정리합니다.
- 스테이징에서 백업을 별도 DB에 복원하고 마이그레이션 후 채점·결과 조회를 시험합니다. 롤백이 필요하면 해당 리비전에서 추가된 데이터와 이전 API의 호환성을 확인한 뒤 복원 또는 전진 수정 중 적합한 방법을 적용합니다.
- 세션 식별자 없이 발급된 기존 액세스 토큰은 갱신이나 재로그인이 필요합니다.

## 검증

```powershell
uv run --locked python -m pytest -q
uv run --locked python -m alembic check
```

테스트가 전용 MySQL 스키마에 마이그레이션과 시드를 적용합니다. 실제 AI 모델로 합성 사례를 두 번씩 채점하려면 다음 명령을 실행합니다.

```powershell
uv run --locked python -m scripts.validate_live_grading --output recheck.json --repetitions 2
```
