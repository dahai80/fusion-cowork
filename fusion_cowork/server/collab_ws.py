"""Collaboration-layer WebSocket bidirectional channel — complements one-way SSE push.

CollabHub rooms by space_id; each WS connection binds (space_id, user_id):
  inbound messages (client→server): chat_send / cursor_move / presence / ping / leave
  outbound broadcasts (server→room): chat / cursor / presence / member_join / member_leave / pong

Integration: SpaceChatService (persisted chat), PresenceManager (online/cursor).
Degradation: websockets not installed → serve_ws raises ImportError, the hub still works for in-process broadcasts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# R-6: per-connection broadcast timeout — give up on slow clients beyond this, don't drag the whole-room broadcast.
_BROADCAST_SEND_TIMEOUT = 5.0
# R-6: per-message chat content cap, prevents giant messages from crushing serialization/broadcast.
_CHAT_CONTENT_MAX = 32 * 1024


class CollabHub:
    def __init__(
        self,
        chat_svc: Any = None,
        presence_manager: Any = None,
        space_store: Any = None,
        auth_token: Optional[str] = None,
        max_per_tenant: int = 50,
    ):
        self._rooms: Dict[str, Set[Any]] = {}
        self._conn_meta: Dict[Any, Dict[str, str]] = {}
        self._chat_svc = chat_svc
        self._presence = presence_manager
        # LO-13: space_store validates membership; auth_token validates hello (prevents unauthenticated injection/presence spoofing)
        self._space_store = space_store
        self._auth_token = auth_token
        # Stage 6: per-tenant connection cap (prevents a single tenant exhausting WS resources), default 50.
        self._max_per_tenant = max(1, int(max_per_tenant))
        self._tenant_conn_count: Dict[str, int] = {}
        logger.debug(f"CollabHub init max_per_tenant={self._max_per_tenant}")

    async def join(self, websocket: Any, space_id: str, user_id: str, display_name: str = "") -> Dict[str, Any]:
        room = self._rooms.setdefault(space_id, set())
        room.add(websocket)
        self._conn_meta[websocket] = {"space_id": space_id, "user_id": user_id, "display_name": display_name}
        if self._presence:
            self._presence.heartbeat(space_id, user_id, display_name=display_name)
        await self._broadcast(
            space_id,
            {
                "type": "member_join",
                "space_id": space_id,
                "user_id": user_id,
                "display_name": display_name,
                "ts": time.time(),
            },
            exclude=websocket,
        )
        await self._send(websocket, {"type": "joined", "space_id": space_id, "user_id": user_id})
        logger.info(f"WS join space={space_id} user={user_id}")
        present = self._presence.list_present(space_id) if self._presence else []
        return {
            "space_id": space_id,
            "user_id": user_id,
            "members_online": [s.to_dict() for s in present],
        }

    async def leave(self, websocket: Any) -> None:
        meta = self._conn_meta.pop(websocket, None)
        if not meta:
            return
        space_id = meta["space_id"]
        user_id = meta["user_id"]
        # Stage 6: per-tenant connection count decrement (_conn_meta recorded tenant_id at join).
        _tid = meta.get("tenant_id")
        if _tid:
            self._tenant_conn_count[_tid] = max(0, self._tenant_conn_count.get(_tid, 0) - 1)
            if self._tenant_conn_count[_tid] == 0:
                self._tenant_conn_count.pop(_tid, None)
        room = self._rooms.get(space_id)
        if room:
            room.discard(websocket)
            if not room:
                self._rooms.pop(space_id, None)
        if self._presence:
            self._presence.remove(space_id, user_id)
        await self._broadcast(
            space_id,
            {
                "type": "member_leave",
                "space_id": space_id,
                "user_id": user_id,
                "ts": time.time(),
            },
        )
        logger.info(f"WS leave space={space_id} user={user_id}")

    async def handle_message(self, websocket: Any, raw: str) -> Optional[Dict[str, Any]]:
        meta = self._conn_meta.get(websocket)
        if not meta:
            return {"error": "未加入房间"}  # i18n: user-facing
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"消息非 JSON: {e}"}  # i18n: user-facing
        mtype = msg.get("type", "")
        space_id = meta["space_id"]
        user_id = meta["user_id"]
        if mtype == "chat_send":
            return await self._handle_chat(websocket, msg, space_id, user_id)
        if mtype == "cursor_move":
            return await self._handle_cursor(websocket, msg, space_id, user_id)
        if mtype == "presence":
            return await self._handle_presence(websocket, msg, space_id, user_id)
        if mtype == "ping":
            await self._send(websocket, {"type": "pong", "ts": time.time()})
            return None
        if mtype == "leave":
            await self.leave(websocket)
            return None
        logger.warning(f"unknown WS message type: {mtype}")
        return {"error": f"未知消息类型: {mtype}"}  # i18n: user-facing

    async def _handle_chat(self, websocket: Any, msg: Dict[str, Any], space_id: str, user_id: str) -> Dict[str, Any]:
        content = str(msg.get("content", "")).strip()
        if not content:
            return {"error": "内容为空"}  # i18n: user-facing
        # R-6: uncapped chat content → a single giant message crushes broadcast (serialization+memory). Capped at 32KiB.
        if len(content) > _CHAT_CONTENT_MAX:
            return {"error": f"内容过长 ({len(content)} > {_CHAT_CONTENT_MAX} 字符)"}  # i18n: user-facing
        if self._chat_svc:
            try:
                await self._chat_svc.send_message(space_id, user_id=user_id, content=content)
            except Exception as e:
                logger.warning(f"chat persistence failed (still broadcasting): {e}")
        msg_id = msg.get("msg_id") or f"msg_{uuid.uuid4().hex[:12]}"
        out = {
            "type": "chat",
            "msg_id": msg_id,
            "space_id": space_id,
            "user_id": user_id,
            "content": content,
            "ts": time.time(),
        }
        await self._broadcast(space_id, out)
        return {"ok": True, "msg_id": msg_id}

    async def _handle_cursor(self, websocket: Any, msg: Dict[str, Any], space_id: str, user_id: str) -> Dict[str, Any]:
        x = float(msg.get("x", 0))
        y = float(msg.get("y", 0))
        target = msg.get("target", "")
        if self._presence:
            self._presence.set_cursor(space_id, user_id, x, y, target=target)
        out = {
            "type": "cursor",
            "space_id": space_id,
            "user_id": user_id,
            "x": x,
            "y": y,
            "target": target,
            "ts": time.time(),
        }
        await self._broadcast(space_id, out, exclude=websocket)
        return {"ok": True}

    async def _handle_presence(
        self, websocket: Any, msg: Dict[str, Any], space_id: str, user_id: str
    ) -> Dict[str, Any]:
        if self._presence:
            self._presence.heartbeat(space_id, user_id, extras=msg.get("extras"))
        out = {
            "type": "presence",
            "space_id": space_id,
            "user_id": user_id,
            "ts": time.time(),
        }
        await self._broadcast(space_id, out, exclude=websocket)
        return {"ok": True}

    async def _broadcast(self, space_id: str, data: Dict[str, Any], exclude: Any = None) -> None:
        room = self._rooms.get(space_id)
        if not room:
            return
        payload = json.dumps(data, ensure_ascii=False)
        targets = [ws for ws in list(room) if ws is not exclude]
        if not targets:
            return

        # R-6: the old sequential await ws.send → one slow client blocked the whole-room broadcast (broadcast storm).
        # Switched to concurrent gather with an independent timeout per connection to isolate slow clients; timeouts don't drag others.
        async def _one(ws: Any) -> bool:
            try:
                await asyncio.wait_for(ws.send(payload), timeout=_BROADCAST_SEND_TIMEOUT)
                return True
            except Exception as e:
                logger.debug(f"broadcast send failed/timed out, marking for removal: {e}")
                return False

        results = await asyncio.gather(*[_one(ws) for ws in targets], return_exceptions=True)
        dead = [ws for ws, ok in zip(targets, results) if ok is False]
        for ws in dead:
            await self.leave(ws)

    async def _send(self, websocket: Any, data: Dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"single send failed: {e}")

    def room_size(self, space_id: str) -> int:
        return len(self._rooms.get(space_id, set()))

    def total_connections(self) -> int:
        return sum(len(r) for r in self._rooms.values())

    async def serve_ws(self, host: str = "127.0.0.1", port: int = 11439) -> Any:
        import websockets

        async def _handler(websocket):
            try:
                # LO-13: hello must carry authentication + identity validation, prevents unauthenticated injection/presence spoofing
                first = await websocket.recv()
                hello = json.loads(first)
                # When auth_token is configured, validate hello.token (Stage 2: JWT also accepted); missing/wrong → reject connection
                _principal = None
                if self._auth_token:
                    from fusion_cowork.auth import verify_any_token

                    token = str(hello.get("token", ""))
                    _principal = verify_any_token(token, self._auth_token)
                    if _principal is None:
                        logger.warning("CollabHub WS auth failed: invalid hello token")
                        await websocket.send(json.dumps({"type": "error", "error": "认证失败"}))  # i18n: user-facing
                        await websocket.close()
                        return
                # Stage 6: per-tenant connection cap — tenant_id from JWT claim or default tenant; over the cap → reject.
                _tenant_id = getattr(_principal, "tenant_id", "") or "default"
                cur = self._tenant_conn_count.get(_tenant_id, 0)
                if cur >= self._max_per_tenant:
                    logger.warning(
                        f"CollabHub WS rejected: tenant={_tenant_id} connections {cur} reached cap {self._max_per_tenant}"
                    )
                    await websocket.send(
                        json.dumps({"type": "error", "error": "租户连接数已达上限"})  # i18n: user-facing
                    )  # i18n: user-facing
                    await websocket.close()
                    return
                self._tenant_conn_count[_tenant_id] = cur + 1
                space_id = str(hello.get("space_id", "")).strip()
                user_id = str(hello.get("user_id", "")).strip()
                if not space_id or not user_id:
                    logger.warning("CollabHub WS rejected: hello missing space_id/user_id")
                    await websocket.send(
                        json.dumps({"type": "error", "error": "缺 space_id/user_id"})  # i18n: user-facing
                    )  # i18n: user-facing
                    await websocket.close()
                    return
                # When space_store is configured, validate membership; non-members are rejected from the room
                if self._space_store is not None:
                    member = await self._space_store.get_member(space_id, user_id)
                    if member is None:
                        space = await self._space_store.get_space(space_id)
                        # A-5: get_space returns a Space dataclass (.owner_id attribute), not a dict —
                        # the old space.get("owner_id") raised AttributeError on the dataclass (non-member path always crashed,
                        # the exception was swallowed by the outer except → connection dropped silently, letting attackers probe space existence).
                        owner_id = getattr(space, "owner_id", "") if space else ""
                        if not space or owner_id != user_id:
                            logger.warning(f"CollabHub WS rejected: user={user_id} not a member of space={space_id}")
                            await websocket.send(
                                json.dumps({"type": "error", "error": "非空间成员"})  # i18n: user-facing
                            )  # i18n: user-facing
                            await websocket.close()
                            return
                await self.join(websocket, space_id, user_id, hello.get("display_name", ""))
                self._conn_meta[websocket]["tenant_id"] = _tenant_id
                async for raw in websocket:
                    await self.handle_message(websocket, raw)
            except Exception as e:
                logger.debug(f"WS connection ended: {e}")
            finally:
                await self.leave(websocket)

        server = await websockets.serve(_handler, host, port)
        logger.info(f"CollabHub WS listening: ws://{host}:{port} (auth={'on' if self._auth_token else 'off'})")
        return server
