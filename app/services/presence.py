import asyncio
import math
from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from app.settings import setting


def finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass
class Player:
    user_id: int
    nickname: str
    level: int
    session_id: str
    x: float
    y: float
    dir: str = "down"
    moving: bool = False
    moves: deque[float] = field(default_factory=deque)
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))
    close_code: int | None = None

    def stop(self, code: int) -> None:
        self.close_code = code
        while not self.outbox.empty():
            self.outbox.get_nowait()
        self.outbox.put_nowait(None)

    def send(self, message: dict) -> None:
        if self.close_code is None:
            try:
                self.outbox.put_nowait(message)
            except asyncio.QueueFull:
                self.stop(1013)

    def pose(self) -> dict:
        return {"x": self.x, "y": self.y, "dir": self.dir}

    def snapshot(self) -> dict:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "level": self.level,
            **self.pose(),
            "moving": self.moving,
        }


class Presence:
    def __init__(self):
        self.players: dict[int, Player] = {}
        self.join_lock = asyncio.Lock()
        self.min_x = float(setting("MAP_MIN_X") or 0)
        self.max_x = float(setting("MAP_MAX_X") or 1920)
        self.min_y = float(setting("MAP_MIN_Y") or 0)
        self.max_y = float(setting("MAP_MAX_Y") or 1408)
        self.spawn_x = float(setting("MAP_SPAWN_X") or 960)
        self.spawn_y = float(setting("MAP_SPAWN_Y") or 880)
        numbers = (
            self.min_x,
            self.max_x,
            self.min_y,
            self.max_y,
            self.spawn_x,
            self.spawn_y,
        )
        if not all(map(math.isfinite, numbers)) or not (
            self.min_x <= self.spawn_x <= self.max_x
            and self.min_y <= self.spawn_y <= self.max_y
        ):
            raise ValueError("Map spawn must be within the finite map bounds.")

    def broadcast(self, message: dict, skip: int) -> None:
        for user_id, player in self.players.items():
            if user_id != skip:
                player.send(message)

    def attach(
        self, user_id: int, nickname: str, level: int, session_id: str
    ) -> Player:
        previous = self.players.get(user_id)
        player = Player(user_id, nickname, level, session_id, self.spawn_x, self.spawn_y)
        player.send(
            {
                "type": "snapshot",
                "me": player.pose(),
                "players": [
                    peer.snapshot()
                    for peer in self.players.values()
                    if peer.user_id != user_id and peer.close_code is None
                ],
            }
        )
        self.players[user_id] = player
        if previous is None:
            self.broadcast(
                {"type": "player_joined", "player": player.snapshot()}, user_id
            )
        else:
            previous.stop(4409)
            self.broadcast(
                {
                    "type": "player_moved",
                    "user_id": user_id,
                    **player.pose(),
                    "moving": False,
                },
                user_id,
            )
        return player

    def detach(self, player: Player) -> None:
        if self.players.get(player.user_id) is player:
            del self.players[player.user_id]
            self.broadcast(
                {"type": "player_left", "user_id": player.user_id}, player.user_id
            )

    def move(self, player: Player, message: dict) -> None:
        if (
            self.players.get(player.user_id) is not player
            or player.close_code is not None
        ):
            return
        x, y, direction, moving = (
            message.get(key) for key in ("x", "y", "dir", "moving")
        )
        if not all(map(finite_number, (x, y))) or direction not in {
            "up",
            "down",
            "left",
            "right",
        }:
            return
        if type(moving) is not bool:
            return
        now = monotonic()
        while player.moves and now - player.moves[0] >= 1:
            player.moves.popleft()
        if len(player.moves) >= 10:
            return
        player.moves.append(now)
        player.x = max(self.min_x, min(x, self.max_x))
        player.y = max(self.min_y, min(y, self.max_y))
        player.dir = direction
        player.moving = moving
        self.broadcast(
            {
                "type": "player_moved",
                "user_id": player.user_id,
                **player.pose(),
                "moving": moving,
            },
            player.user_id,
        )

    async def revoke_session(self, session_id: str) -> None:
        async with self.join_lock:
            for player in tuple(self.players.values()):
                if player.session_id == session_id:
                    player.stop(4401)

    def update_level(self, user_id: int, level: int) -> None:
        player = self.players.get(user_id)
        if player is not None and level > player.level:
            player.level = level
            self.broadcast(
                {"type": "player_updated", "user_id": user_id, "level": player.level},
                user_id,
            )
