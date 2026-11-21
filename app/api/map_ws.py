import asyncio
import json

from anyio import create_task_group
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.errors import ApiError
from app.models import User
from app.services.auth import access_identity, access_session_is_active
from app.services.presence import Player, Presence
from app.settings import setting

router = APIRouter()
MAX_MESSAGE_BYTES = 2048
SESSION_CHECK_SECONDS = 15


class PayloadTooLarge(Exception):
    pass


def allowed_origin(socket: WebSocket) -> bool:
    origin = socket.headers.get("origin")
    if not origin:
        return True
    configured = setting("WS_ALLOWED_ORIGINS")
    if configured:
        return origin in {item.strip() for item in configured.split(",") if item.strip()}
    host = socket.headers.get("host")
    scheme = "https" if socket.url.scheme == "wss" else "http"
    return origin == f"{scheme}://{host}"


async def receive_json(socket: WebSocket):
    body = await socket.receive_text()
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise PayloadTooLarge
    return json.loads(body)


def identity(state, token: str) -> tuple[int, str, int, str]:
    if not state.jwt_secret:
        raise ApiError("INVALID_TOKEN")
    user_id, session_id = access_identity(token, state.jwt_secret, state.clock())
    with state.session_factory() as session:
        if not access_session_is_active(session, user_id, session_id, state.clock()):
            raise ApiError("INVALID_TOKEN")
        user = session.get(User, user_id)
        if user is None:
            raise ApiError("INVALID_TOKEN")
        return user.id, user.nickname, user.level, session_id


async def read(socket: WebSocket, room: Presence, player: Player) -> None:
    while True:
        try:
            message = await receive_json(socket)
        except PayloadTooLarge:
            await socket.close(code=1009)
            return
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(message, dict) and message.get("type") == "move":
            room.move(player, message)


async def write(socket: WebSocket, player: Player) -> None:
    while True:
        message = await player.outbox.get()
        if message is None:
            await asyncio.wait_for(
                socket.close(code=player.close_code or 1000), timeout=5
            )
            return
        await asyncio.wait_for(socket.send_json(message), timeout=5)


async def watch_session(
    socket: WebSocket, state, player: Player, token: str
) -> None:
    while player.close_code is None:
        await asyncio.sleep(SESSION_CHECK_SECONDS)
        try:
            await run_in_threadpool(identity, state, token)
        except ApiError:
            player.close_code = 4401
            await socket.close(code=4401)
            return


@router.websocket("/ws/map")
async def map_socket(socket: WebSocket):
    await socket.accept()
    if not allowed_origin(socket):
        await socket.close(code=4403)
        return
    state = socket.app.state
    room = state.presence
    try:
        first = await asyncio.wait_for(receive_json(socket), timeout=5)
        if (
            not isinstance(first, dict)
            or first.get("type") != "auth"
            or not isinstance(first.get("access_token"), str)
        ):
            raise ApiError("INVALID_TOKEN")
        async with room.join_lock:
            user = await run_in_threadpool(identity, state, first["access_token"])
            player = room.attach(*user)
    except WebSocketDisconnect:
        return
    except PayloadTooLarge:
        await socket.close(code=1009)
        return
    except (ApiError, asyncio.TimeoutError, json.JSONDecodeError, KeyError, TypeError):
        await socket.close(code=4401)
        return

    try:
        async with create_task_group() as group:

            async def run(worker, *arguments):
                try:
                    await worker(*arguments)
                except (WebSocketDisconnect, OSError, asyncio.TimeoutError):
                    pass
                finally:
                    group.cancel_scope.cancel()

            group.start_soon(run, read, socket, room, player)
            group.start_soon(run, write, socket, player)
            group.start_soon(
                run, watch_session, socket, state, player, first["access_token"]
            )
    finally:
        room.detach(player)
