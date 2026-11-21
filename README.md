# youthgarten API

FastAPI와 MySQL 8 기반의 AI 교육 플랫폼 백엔드입니다. 필수 강의의 중복 제외 시청 구간이 80%에 도달하면 실무 과제에 응시할 수 있습니다. 채점 결과는 실무 과제 해결과 AI 활용의 점수·충족 여부, 필수 요건 판정, 근거를 제공합니다.

- [API 명세서](docs/2_API명세서.md) · [ERD](docs/1_ERD.md) · [백엔드 MVP 개발 계획](docs/백엔드_MVP_개발계획.md)
- [백엔드 개선 계획](docs/superpowers/plans/2026-11-20-backend-improvement.md)
- [실제 AI 채점 검증](docs/실제_AI_채점_검증.md) · [채점 결과 JSON](docs/실제_AI_채점_결과.json)

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
- `scripts.seed`는 개발·테스트용입니다. 신규 시드는 무역 사무 직무, 모의 강의 1개, 납기 지연 회신 과제 1개와 자료 3개를 만듭니다. 시범 운영에는 아래 카탈로그 게시 명령을 사용합니다.

## 환경 변수

| 이름 | 사용 방법 |
|---|---|
| `DATABASE_URL` · `TEST_DATABASE_URL` | 개발·테스트 DB의 `mysql+pymysql://` DSN |
| `JWT_SECRET` · `SECURE_COOKIES` | 최소 32바이트의 JWT 서명 키. 쿠키는 기본적으로 Secure이며, 로컬 HTTP 개발에서만 `SECURE_COOKIES=0`을 명시합니다. |
| `AI_BASE_URL` · `AI_MODEL` · `AI_API_KEY` | Chat Completions 호환 기본 URL, JSON Schema 응답을 지원하는 모델, 제공자 키. OpenAI 기본 URL에서는 `OPENAI_API_KEY`도 사용 가능 |
| `AI_TIMEOUT_SECONDS` | 모델 HTTP 연결·읽기 제한시간. 기본 45초, 1~120초 |
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

## 실제 AI 모델

별도로 실행하는 Chat Completions 호환 서버를 `AI_BASE_URL`로 연결합니다. 확인한 로컬 조합은 llama.cpp `b12556`과 Qwen3-4B-Instruct-2507 Q4_K_M입니다. [llama.cpp 릴리스](https://github.com/ggml-org/llama.cpp/releases/tag/b12556) · [검증한 가중치 버전](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/tree/a06e946bb6b655725eafa393f4a9745d460374c9)

가중치의 실제 경로를 지정해 로컬 모델 서버를 실행합니다.

```powershell
.\llama-server.exe -m 'C:\path\to\Qwen3-4B-Instruct-2507-Q4_K_M.gguf' --alias qwen3-4b-instruct-2507 --host 127.0.0.1 --port 8091 -c 8192 -np 1 --temp 0 --seed 23 -n 800
```

이 구성에서는 `AI_BASE_URL=http://127.0.0.1:8091/v1`과 `AI_MODEL=qwen3-4b-instruct-2507`을 설정합니다. 제공자가 응답하지 않을 때 대화 API는 `AI_UNAVAILABLE`을 반환합니다. MySQL에 제출과 채점 작업을 함께 저장하므로 API를 다시 시작해도 작업을 이어갈 수 있습니다. 맵 상태와 요청 한도는 프로세스 내에서 관리하므로 초기 배포의 Uvicorn 워커는 1개로 실행합니다.

## 배포·운영

- Compose를 사용할 때는 별도 파일에 `MYSQL_APP_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `JWT_SECRET`, 모델 설정을 넣고 `docker compose --env-file <환경 파일> up --build`를 실행합니다. DB 접속 URL의 비밀번호에는 URL 안전 문자를 사용합니다. Compose는 MySQL 8과 마이그레이션을 먼저 실행한 뒤 API를 루프백에 엽니다.
- HTTPS 프록시에서 프론트 정적 파일과 `/api`를 같은 출처로 제공하고 WebSocket Upgrade를 전달합니다. 신뢰할 프록시 주소와 브라우저 출처를 제한합니다. 모델 서버 주소는 API 프로세스나 컨테이너에서 연결할 수 있어야 합니다.
- `/health/live`는 프로세스, `/health/ready`는 DB 연결, `/health/grading`은 채점 작업 상태·모델 설정 여부를 확인합니다. 실제 모델 응답 검증은 아래 별도 명령으로 실행합니다.
- `uv run --locked python -m scripts.prune_sessions`로 만료 7일이 지난 토큰의 정리 대상을 확인합니다. 운영 보존 정책에 맞춰 `--apply`를 지정하면 한 번에 기본 1,000행을 정리합니다.
- 스테이징에서 백업을 별도 DB에 복원하고 마이그레이션 후 채점·결과 조회를 시험합니다. 롤백이 필요하면 해당 리비전에서 추가된 데이터와 이전 API의 호환성을 확인한 뒤 복원 또는 전진 수정 중 적합한 방법을 적용합니다.
- 세션 식별자 없이 발급된 기존 액세스 토큰은 갱신이나 재로그인이 필요합니다.

## 검증

```powershell
uv run --locked python -m pytest -q
uv run --locked python -m alembic check
```

테스트가 전용 MySQL 스키마에 마이그레이션과 시드를 적용합니다. 실제 AI 모델과 함께 네 가지 합성 사례를 두 번씩 다시 채점하려면 다음 명령을 실행합니다.

```powershell
uv run --locked python -m scripts.validate_live_grading --output recheck.json --repetitions 2
```
