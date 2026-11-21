import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.parametrize("secret", [None, "short", " " * 32])
def test_startup_rejects_missing_or_weak_signing_keys(api_engine, monkeypatch, secret):
    monkeypatch.setattr("app.main.setting", {"JWT_SECRET": secret}.get)
    with (
        pytest.raises(RuntimeError, match="JWT_SECRET"),
        TestClient(create_app(api_engine)),
    ):
        pass


def test_secure_refresh_cookie_is_default(api_engine, monkeypatch, register):
    _, credentials, _ = register()
    monkeypatch.setattr(
        "app.main.setting", {"JWT_SECRET": "test-only-valid-signing-key-32-bytes"}.get
    )
    app = create_app(api_engine)
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post("/api/v1/auth/login", json=credentials)
    assert login.status_code == 200
    assert "secure" in login.headers["set-cookie"].lower()


def test_startup_rejects_invalid_cookie_flag(api_engine, monkeypatch):
    monkeypatch.setattr(
        "app.main.setting",
        {
            "JWT_SECRET": "test-only-valid-signing-key-32-bytes",
            "SECURE_COOKIES": "unknown",
        }.get,
    )
    with (
        pytest.raises(RuntimeError, match="SECURE_COOKIES"),
        TestClient(create_app(api_engine)),
    ):
        pass
