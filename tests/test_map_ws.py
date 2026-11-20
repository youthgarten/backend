import pytest
from starlette.websockets import WebSocketDisconnect
from support import FixtureProvider, submit_with_evidence

PATH = "/api/v1/ws/map"


def connect(socket, login):
    socket.send_json({"type": "auth", "access_token": login.json()["access_token"]})
    return socket.receive_json()


@pytest.fixture(autouse=True)
def phaser_map(application, monkeypatch):
    from app.services.presence import Presence

    for key, value in {
        "MAP_MIN_X": "0",
        "MAP_MAX_X": "1920",
        "MAP_MIN_Y": "0",
        "MAP_MAX_Y": "1408",
        "MAP_SPAWN_X": "960",
        "MAP_SPAWN_Y": "880",
    }.items():
        monkeypatch.setenv(key, value)
    application.state.presence = Presence()


def test_presence_pose_replacement_and_cleanup(client, register):
    first, _, login_a = register()
    second, _, login_b = register()
    with client.websocket_connect(PATH) as a:
        initial = connect(a, login_a)
        assert initial["players"] == []
        assert initial["me"] == {"x": 960.0, "y": 880.0, "dir": "down"}
        with client.websocket_connect(PATH) as b:
            player = connect(b, login_b)["players"][0]
            assert player["user_id"] == first["id"]
            assert player["y"] == 880.0 and "z" not in player
            joined = a.receive_json()
            assert joined["type"] == "player_joined"
            assert joined["player"]["user_id"] == second["id"]
            assert joined["player"]["y"] == 880.0 and "z" not in joined["player"]

            b.send_json({"type": "move", "x": 1, "z": 1, "dir": "right", "moving": True})
            b.send_json(
                {"type": "move", "x": 980, "y": 900, "dir": "right", "moving": True}
            )
            assert a.receive_json() == {
                "type": "player_moved",
                "user_id": second["id"],
                "x": 980,
                "y": 900,
                "dir": "right",
                "moving": True,
            }

            b.send_json(
                {"type": "move", "x": 3000, "y": -1000, "dir": "up", "moving": True}
            )
            pose = a.receive_json()
            assert pose == {
                "type": "player_moved",
                "user_id": second["id"],
                "x": 1920.0,
                "y": 0.0,
                "dir": "up",
                "moving": True,
            }
            with client.websocket_connect(PATH) as replacement:
                assert (
                    connect(replacement, login_b)["players"][0]["user_id"]
                    == first["id"]
                )
                with pytest.raises(WebSocketDisconnect) as closed:
                    b.receive_json()
                assert closed.value.code == 4409
                assert a.receive_json() == {
                    "type": "player_moved",
                    "user_id": second["id"],
                    "x": 960.0,
                    "y": 880.0,
                    "dir": "down",
                    "moving": False,
                }
            assert a.receive_json() == {"type": "player_left", "user_id": second["id"]}


def test_invalid_first_message_closes_with_auth_code(client):
    with client.websocket_connect(PATH) as socket:
        socket.send_json({"type": "move", "x": 1})
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4401


def test_cross_origin_map_connection_is_rejected(client, monkeypatch):
    monkeypatch.delenv("WS_ALLOWED_ORIGINS", raising=False)
    with client.websocket_connect(
        PATH, headers={"Origin": "https://unexpected.example"}
    ) as socket, pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    assert closed.value.code == 4403


def test_invalid_direction_is_ignored(client, register):
    _, _, login_a = register()
    _, _, login_b = register()
    with client.websocket_connect(PATH) as observer:
        connect(observer, login_a)
        with client.websocket_connect(PATH) as mover:
            connect(mover, login_b)
            observer.receive_json()
            mover.send_json(
                {"type": "move", "x": 980, "y": 900, "dir": "diagonal", "moving": True}
            )
            mover.send_json(
                {"type": "move", "x": 981, "y": 900, "dir": "left", "moving": True}
            )
            assert observer.receive_json()["dir"] == "left"


def test_logout_closes_only_the_matching_map_session(client, register):
    _, credentials, first_login = register()
    second_login = client.post("/api/v1/auth/login", json=credentials)
    first_cookie = first_login.cookies["refresh_token"]

    with client.websocket_connect(PATH) as first:
        connect(first, first_login)
        logout = client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": f"refresh_token={first_cookie}"},
        )
        assert logout.status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            first.receive_json()
        assert closed.value.code == 4401

    with client.websocket_connect(PATH) as second:
        assert connect(second, second_login)["type"] == "snapshot"


def test_session_watch_closes_an_expired_connection(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.api import map_ws
    from app.errors import ApiError

    monkeypatch.setattr(map_ws, "SESSION_CHECK_SECONDS", 0.01)

    def expired(*_):
        raise ApiError("TOKEN_EXPIRED")

    monkeypatch.setattr(map_ws, "identity", expired)
    player = SimpleNamespace(close_code=None)
    socket = SimpleNamespace(close=lambda **_: asyncio.sleep(0))
    asyncio.run(map_ws.watch_session(socket, None, player, "expired"))
    assert player.close_code == 4401


def test_oversized_map_message_closes_the_connection(client, register):
    _, _, login = register()
    with client.websocket_connect(PATH) as socket:
        connect(socket, login)
        socket.send_text("x" * 2049)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1009


def test_grading_publishes_level_after_commit(client, application, register, ready):
    examinee, headers, task = ready
    login_token = headers["Authorization"].split()[-1]
    _, _, observer = register()
    application.state.provider = FixtureProvider()
    with client.websocket_connect(PATH) as first:
        first.send_json({"type": "auth", "access_token": login_token})
        assert first.receive_json()["type"] == "snapshot"
        with client.websocket_connect(PATH) as second:
            connect(second, observer)
            first.receive_json()
            path = submit_with_evidence(client, headers, task["id"])
            assert second.receive_json() == {
                "type": "player_updated",
                "user_id": examinee["id"],
                "level": 2,
            }
            assert client.get(path, headers=headers).json()["level_up"]["level"] == 2


def test_a_level_commit_during_map_join_is_not_lost(
    client, application, ready, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from app.api import map_ws
    from app.services import grading

    user, headers, task = ready
    application.state.provider = FixtureProvider()
    attempt = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    ).json()["id"]
    path = f"/api/v1/attempts/{attempt}"
    assert (
        client.post(
            path + "/messages", headers=headers, json={"content": "확정일 확인"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            path + "/messages",
            headers=headers,
            json={"content": "일정표와 대조해 11월 28일이 확정임을 확인했습니다."},
        ).status_code
        == 201
    )
    read_identity, release, committed = Event(), Event(), Event()
    identity = map_ws.identity
    finish = grading.finish_queued_grading

    def paused_identity(*args):
        value = identity(*args)
        read_identity.set()
        if not release.wait(timeout=8):
            raise TimeoutError("Test did not release the map identity lookup.")
        return value

    def observed_finish(*args):
        value = finish(*args)
        committed.set()
        return value

    monkeypatch.setattr(map_ws, "identity", paused_identity)
    monkeypatch.setattr(grading, "finish_queued_grading", observed_finish)
    with (
        client.websocket_connect(PATH) as socket,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        socket.send_json(
            {"type": "auth", "access_token": headers["Authorization"].split()[-1]}
        )
        assert read_identity.wait(timeout=5)
        submission = executor.submit(
            client.post,
            path + "/submit",
            headers=headers,
            json={"answer": "Confirmed November 28."},
        )
        try:
            assert committed.wait(timeout=5)
            try:
                submission.result(timeout=0.4)
            except TimeoutError:
                pass
        finally:
            release.set()
        assert socket.receive_json()["type"] == "snapshot"
        assert submission.result(timeout=5).status_code == 202
        assert application.state.presence.players[user["id"]].level == 2
