import json
import os
import subprocess
from pathlib import Path

import pytest

from app.services import ai


@pytest.fixture
def cli_provider(monkeypatch, tmp_path):
    executable = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    executable.touch(mode=0o700)
    values = {
        "AI_PROVIDER": "codex_cli",
        "CODEX_CLI_PATH": str(executable),
        "CODEX_CLI_MODEL": "test-model",
        "AI_BASE_URL": "http://127.0.0.1:8091/v1",
        "AI_MODEL": "stale-http-model",
        "AI_TIMEOUT_SECONDS": "17",
    }
    monkeypatch.setattr(ai, "setting", values.get)
    return ai.configured_provider(), values


def test_codex_cli_uses_an_isolated_process_for_chat(cli_provider, monkeypatch):
    provider, values = cli_provider
    workspace = None
    message = "이 문자열은 명령줄 인자가 아닌 대화 데이터입니다. & echo unsafe"
    instruction = '실무 AI 도우미로서 "사용자 질문"에 답한다.'
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")

    def run(args, **kwargs):
        nonlocal workspace
        workspace = Path(kwargs["cwd"])
        assert args[0] == values["CODEX_CLI_PATH"]
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[args.index("-a") + 1] == "never"
        assert args[args.index("--model") + 1] == "test-model"
        assert {"--ephemeral", "--ignore-user-config"} <= set(args)
        disabled = {args[i + 1] for i, arg in enumerate(args) if arg == "--disable"}
        assert {"shell_tool", "hooks", "apps", "plugins", "multi_agent", "multi_agent_v2"} <= disabled
        assert "--output-schema" not in args
        assert args[-1] == "-"
        assert all(message not in arg for arg in args)
        developer, = (
            arg.removeprefix("developer_instructions=")
            for arg in args if arg.startswith("developer_instructions=")
        )
        assert instruction in json.loads(developer)
        assert json.loads(kwargs["input"]) == [{"role": "user", "content": message}]
        assert kwargs["timeout"] == 17
        assert kwargs["shell"] is False
        assert {"JWT_SECRET", "OPENAI_API_KEY"}.isdisjoint(kwargs["env"])
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        output = Path(args[args.index("--output-last-message") + 1])
        assert output.parent == workspace
        output.write_text("업무 자동화 답변", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", run)
    assert ai.provider_is_configured(provider)
    assert provider.model == "test-model"
    assert provider.chat([
        {"role": "system", "content": instruction},
        {"role": "user", "content": message},
    ]) == "업무 자동화 답변"
    assert not workspace.exists()


def test_codex_cli_passes_only_the_schema_to_structured_output(cli_provider, monkeypatch):
    provider, _ = cli_provider
    schema = {
        "type": "object",
        "properties": {"satisfied": {"type": "boolean"}},
        "required": ["satisfied"],
        "additionalProperties": False,
    }

    def run(args, **kwargs):
        schema_path = Path(args[args.index("--output-schema") + 1])
        output = Path(args[args.index("--output-last-message") + 1])
        assert schema_path.parent == output.parent == kwargs["cwd"]
        assert json.loads(schema_path.read_text(encoding="utf-8")) == schema
        output.write_text('{"satisfied":true}', encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", run)
    response = provider.complete(
        [], response_format={"type": "json_schema", "json_schema": {"schema": schema}}
    )
    assert json.loads(response) == {"satisfied": True}


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing", "invalid", "oversize"])
def test_codex_cli_maps_process_and_output_errors(cli_provider, monkeypatch, failure):
    provider, _ = cli_provider
    workspace = None

    def run(args, **kwargs):
        nonlocal workspace
        workspace = Path(kwargs["cwd"])
        output = Path(args[args.index("--output-last-message") + 1])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 17, stderr=b"private model output")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, args, stderr=b"private model output")
        if failure == "invalid":
            output.write_bytes(b"\xff")
        if failure == "oversize":
            output.write_bytes(b"x" * (ai.MAX_RESPONSE_BYTES + 1))

    monkeypatch.setattr(subprocess, "run", run)
    error_type = ai.ProviderInputError if failure == "oversize" else ai.ProviderError
    with pytest.raises(error_type) as exc:
        provider.chat([])
    assert type(exc.value) is error_type
    assert "private model output" not in str(exc.value)
    assert not workspace.exists()


def test_codex_cli_rejects_oversized_input_before_starting(cli_provider, monkeypatch):
    provider, _ = cli_provider

    def run(*args, **kwargs):
        pytest.fail("Oversized input must not start a process")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ai.ProviderInputError, match="input is too large"):
        provider.chat([{"role": "user", "content": "가" * ai.MAX_REQUEST_BYTES}])


def test_codex_cli_does_not_fall_back_to_http_when_not_installed(cli_provider):
    _, values = cli_provider
    values["CODEX_CLI_PATH"] = "relative-codex.exe"
    provider = ai.configured_provider()
    assert not ai.provider_is_configured(provider)
    with pytest.raises(ai.ProviderInputError, match="CODEX_CLI_PATH"):
        provider.chat([])
