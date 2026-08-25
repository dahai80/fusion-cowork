"""协作层 WebSocket 双向通道 — 补 SSE 单向推送。

CollabHub 按 space_id 分房间, 每条 WS 连接绑定 (space_id, user_id):
  入站消息 (client→server): chat_send / cursor_move / presence / ping / leave
  出站广播 (server→room): chat / cursor / presence / member_join / member_leave / pong

集成: SpaceChatService (持久化聊天), PresenceManager (在线/光标)。
降级: websockets 未安装 → serve_ws 抛 ImportError, hub 仍可用于进程内广播。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class CollabHub:
    def __init__(
        self,
        chat_svc: Any = None,
        presence_manager: Any = None,
        space_store: Any = None,
        auth_token: Optional[str] = None,
    ):
        self._rooms: Dict[str, Set[Any]] = {}
        self._conn_meta: Dict[Any, Dict[str, str]] = {}
        self._chat_svc = chat_svc
        self._presence = presence_manager
        # LO-13: space_store 校验成员资格; auth_token 校验 hello (防无认证注入/presence 冒充)
        self._space_store = space_store
        self._auth_token = auth_token
        logger.debug("CollabHub 初始化")

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
            return {"error": "未加入房间"}
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"消息非 JSON: {e}"}
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
        logger.warning(f"未知 WS 消息类型: {mtype}")
        return {"error": f"未知消息类型: {mtype}"}

    async def _handle_chat(self, websocket: Any, msg: Dict[str, Any], space_id: str, user_id: str) -> Dict[str, Any]:
        content = str(msg.get("content", "")).strip()
        if not content:
            return {"error": "内容为空"}
        if self._chat_svc:
            try:
                await self._chat_svc.send_message(space_id, user_id=user_id, content=content)
            except Exception as e:
                logger.warning(f"聊天持久化失败 (仍广播): {e}")
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
        dead: List[Any] = []
        for ws in list(room):
            if ws is exclude:
                continue
            try:
                await ws.send(payload)
            except Exception as e:
                logger.debug(f"广播发送失败, 标记移除: {e}")
                dead.append(ws)
        for ws in dead:
            await self.leave(ws)

    async def _send(self, websocket: Any, data: Dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"单发失败: {e}")

    def room_size(self, space_id: str) -> int:
        return len(self._rooms.get(space_id, set()))

    def total_connections(self) -> int:
        return sum(len(r) for r in self._rooms.values())

    async def serve_ws(self, host: str = "127.0.0.1", port: int = 11439) -> Any:
        import websockets

        async def _handler(websocket):
            try:
                # LO-13: hello 必须带认证 + 身份校验, 防无认证注入/presence 冒充
                first = await websocket.recv()
                hello = json.loads(first)
                # auth_token 配了则校验 hello.token, 缺/错拒连
                if self._auth_token:
                    token = str(hello.get("token", ""))
                    if token != self._auth_token:
                        logger.warning("CollabHub WS 认证失败: hello token 无效")
                        await websocket.send(json.dumps({"type": "error", "error": "认证失败"}))
                        await websocket.close()
                        return
                space_id = str(hello.get("space_id", "")).strip()
                user_id = str(hello.get("user_id", "")).strip()
                if not space_id or not user_id:
                    logger.warning("CollabHub WS 拒绝: hello 缺 space_id/user_id")
                    await websocket.send(json.dumps({"type": "error", "error": "缺 space_id/user_id"}))
                    await websocket.close()
                    return
                # space_store 配了则校验成员资格, 非成员拒入房
                if self._space_store is not None:
                    member = await self._space_store.get_member(space_id, user_id)
                    if member is None:
                        space = await self._space_store.get_space(space_id)
                        # A-5: get_space 返回 Space dataclass (.owner_id 属性), 非 dict —
                        # 旧 space.get("owner_id") 在 dataclass 上 AttributeError (非成员路径恒崩,
                        # 异常被外层 except 吞 → 连接静默断, 攻击者可探测空间存在性)。
                        owner_id = getattr(space, "owner_id", "") if space else ""
                        if not space or owner_id != user_id:
                            logger.warning(f"CollabHub WS 拒绝: user={user_id} 非 space={space_id} 成员")
                            await websocket.send(json.dumps({"type": "error", "error": "非空间成员"}))
                            await websocket.close()
                            return
                await self.join(websocket, space_id, user_id, hello.get("display_name", ""))
                async for raw in websocket:
                    await self.handle_message(websocket, raw)
            except Exception as e:
                logger.debug(f"WS 连接结束: {e}")
            finally:
                await self.leave(websocket)

        server = await websockets.serve(_handler, host, port)
        logger.info(f"CollabHub WS 监听: ws://{host}:{port} (auth={'on' if self._auth_token else 'off'})")
        return server
