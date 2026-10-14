import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from jose import JWTError, jwt
from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import User, ElderProfile

ws_router = APIRouter(tags=["websocket"])


class ConnectionManager:
    def __init__(self):
        # Room keyed by elder_id or "all" -> set of WebSockets
        self.rooms: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = set()
        self.rooms[room_id].add(websocket)

    def disconnect(self, websocket: WebSocket, room_id: str):
        if room_id in self.rooms:
            self.rooms[room_id].discard(websocket)
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast_to_elder(self, elder_id: str, event_type: str, data: dict, ts: Optional[str] = None):
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        message = {
            "type": event_type,
            "elder_id": elder_id,
            "ts": ts,
            "data": data,
        }
        text_data = json.dumps(message)

        # Send to specific elder room
        sockets_to_send = list(self.rooms.get(elder_id, set()))
        # Also send to multiplex room "all"
        sockets_to_send.extend(list(self.rooms.get("all", set())))

        dead_sockets = []
        for ws in set(sockets_to_send):
            try:
                await ws.send_text(text_data)
            except Exception:
                dead_sockets.append(ws)

        for ws in dead_sockets:
            for r_id in list(self.rooms.keys()):
                if ws in self.rooms[r_id]:
                    self.disconnect(ws, r_id)


manager = ConnectionManager()


async def verify_ws_token(token: Optional[str], elder_id: str) -> Optional[User]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            return None
    except JWTError:
        return None

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            return None

        if elder_id == "all":
            if user.role != "caregiver":
                return None
            return user

        profile = (await db.execute(select(ElderProfile).where(ElderProfile.user_id == elder_id))).scalar_one_or_none()
        if not profile:
            return None

        if user.role == "caregiver":
            if profile.caregiver_id != user.id:
                return None
        elif user.role == "elder":
            if user.id != elder_id:
                return None
        else:
            return None

        return user


@ws_router.websocket("/ws/{elder_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    elder_id: str,
    token: Optional[str] = Query(None)
):
    if not token:
        await websocket.close(code=4401)
        return

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            await websocket.close(code=4401)
            return
    except JWTError:
        await websocket.close(code=4401)
        return

    user = await verify_ws_token(token, elder_id)
    if not user:
        await websocket.close(code=4403)
        return

    await manager.connect(websocket, elder_id)

    # Ping task to survive proxy idle timeouts (25 seconds)
    async def keep_alive():
        while True:
            try:
                await asyncio.sleep(25)
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break

    ping_task = asyncio.create_task(keep_alive())

    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                msg = json.loads(data_text)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ping_task.cancel()
        manager.disconnect(websocket, elder_id)
