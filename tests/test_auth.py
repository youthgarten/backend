from concurrent.futures import ThreadPoolExecutor

from support import bearer


def test_signup_login_profile_and_tutorial(client, register):
    user, credentials, login = register("가" * 64)
    assert user["level"] == 1
    assert login.json()["expires_in"] == 3600
    cookie = login.headers["set-cookie"].lower()
    assert all(
        value in cookie
        for value in [
            "httponly",
            "samesite=lax",
            "path=/api/v1/auth",
            "max-age=1209600",
        ]
    )
    headers = bearer(login)
    profile = client.get("/api/v1/users/me", headers=headers).json()
    assert profile["id"] == user["id"] and not profile["tutorial_seen"]
    assert profile["passed_task_count"] == 0
    assert client.patch(
        "/api/v1/users/me", headers=headers, json={"tutorial_seen": True}
    ).json()["tutorial_seen"]
    already = client.post(
        "/api/v1/auth/signup",
        json={**credentials, "nickname": "지원", "job_id": user["job"]["id"]},
    )
    assert (already.status_code, already.json()["error"]["code"]) == (
        409,
        "LOGIN_ID_TAKEN",
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={**credentials, "password": "bad"}
        ).json()["error"]["code"]
        == "INVALID_CREDENTIALS"
    )


def test_expiry_and_refresh_cookie_is_consumed_once(client, register, clock):
    _, _, login = register()
    headers = bearer(login)
    clock.advance(hours=1)
    assert (
        client.get("/api/v1/users/me", headers=headers).json()["error"]["code"]
        == "TOKEN_EXPIRED"
    )
    cookie = login.cookies["refresh_token"]

    def refresh():
        return client.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={cookie}"}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: refresh(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 401]
    new_cookie = next(
        response.cookies["refresh_token"]
        for response in responses
        if response.status_code == 200
    )
    logout = client.post(
        "/api/v1/auth/logout", headers={"Cookie": f"refresh_token={new_cookie}"}
    )
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    rejected = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={new_cookie}"}
    )
    assert rejected.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


def test_invalid_credentials_always_perform_a_password_check(
    client, register, monkeypatch
):
    from app.api import auth

    _, credentials, _ = register()
    seen = []
    original = auth.check_password

    def counted(password, stored_hash):
        seen.append(stored_hash)
        return original(password, stored_hash)

    monkeypatch.setattr(auth, "check_password", counted)
    known = client.post("/api/v1/auth/login", json={**credentials, "password": "wrong"})
    missing = client.post(
        "/api/v1/auth/login", json={"login_id": "missing_user", "password": "wrong"}
    )
    assert known.status_code == missing.status_code == 401
    assert known.json() == missing.json()
    assert len(seen) == 2


def test_logout_with_a_rotated_cookie_revokes_its_session_only(client, register):
    _, credentials, original = register()
    a = original.cookies["refresh_token"]
    separate = client.post("/api/v1/auth/login", json=credentials).cookies[
        "refresh_token"
    ]
    rotated = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={a}"}
    )
    b = rotated.cookies["refresh_token"]
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"Cookie": f"refresh_token={a}"}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={b}"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={separate}"}
        ).status_code
        == 200
    )


def test_logout_immediately_revokes_that_access_token_only(client, register):
    _, credentials, first = register()
    second = client.post("/api/v1/auth/login", json=credentials)
    first_headers = bearer(first)
    second_headers = bearer(second)

    assert client.get("/api/v1/users/me", headers=first_headers).status_code == 200
    assert client.get("/api/v1/users/me", headers=second_headers).status_code == 200

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": f"refresh_token={first.cookies['refresh_token']}"},
    )
    assert logout.status_code == 204
    rejected = client.get("/api/v1/users/me", headers=first_headers)
    assert (rejected.status_code, rejected.json()["error"]["code"]) == (
        401,
        "INVALID_TOKEN",
    )
    assert client.get("/api/v1/users/me", headers=second_headers).status_code == 200


def test_delayed_reuse_of_rotated_refresh_token_revokes_its_family(
    client, register, clock
):
    _, _, login = register()
    original = login.cookies["refresh_token"]
    rotated = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"refresh_token={original}"},
    )
    current = rotated.cookies["refresh_token"]
    clock.advance(seconds=11)

    replay = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"refresh_token={original}"},
    )
    assert replay.status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={current}"},
        ).status_code
        == 401
    )
