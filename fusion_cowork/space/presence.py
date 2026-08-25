"""成员 presence + 实时光标 — 协作空间实时状态层。

Presence: 成员在线/离线状态, 基于 heartbeat 时间戳判定 (超时阈值默认 60s)。
Cursor: 成员实时光标位置 (x, y, target_id), 内存态, 不持久化。

事件通过 EventEmitter 推送: space:{id}:presence, space:{id}:cursor。
降级: EventEmitter 未注入时仅维护内存态, 不推送 (静默)。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PRESENCE_TIMEOUT = 60.0


@dataclass
class PresenceState:
    user_id: str = ""
    display_name: str = ""
    online: bool = False
    last_heartbeat: float = 0.0
    cursor_x: float = 0.0
    cursor_y: float = 0.0
    cursor_target: str = ""
    cursor_ts: float = 0.0
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "online": self.online,
            "last_heartbeat": self.last_heartbeat,
            "cursor_x": self.cursor_x,
            "cursor_y": self.cursor_y,
            "cursor_target": self.cursor_target,
            "cursor_ts": self.cursor_ts,
            "extras": self.extras,
        }


class PresenceManager:
    def __init__(self, event_emitter: Any = None, timeout: float = DEFAULT_PRESENCE_TIMEOUT):
        self._states: Dict[str, Dict[str, PresenceState]] = {}
        self._emitter = event_emitter
        self._timeout = timeout
        logger.debug(f"PresenceManager 初始化, timeout={timeout}s")

    def heartbeat(
        self, space_id: str, user_id: str, display_name: str = "", extras: Optional[Dict[str, Any]] = None
    ) -> PresenceState:
        states = self._states.setdefault(space_id, {})
        st = states.get(user_id)
        now = time.time()
        if st is None:
            st = PresenceState(user_id=user_id, display_name=display_name)
            states[user_id] = st
        if display_name:
            st.display_name = display_name
        if extras:
            st.extras.update(extras)
        st.online = True
        st.last_heartbeat = now
        self._emit(space_id, "presence", st.to_dict())
        logger.debug(f"heartbeat space={space_id} user={user_id}")
        return st

    def set_cursor(self, space_id: str, user_id: str, x: float, y: float, target: str = "") -> PresenceState:
        states = self._states.setdefault(space_id, {})
        st = states.get(user_id)
        if st is None:
            st = PresenceState(user_id=user_id)
            states[user_id] = st
        st.cursor_x = float(x)
        st.cursor_y = float(y)
        st.cursor_target = target
        st.cursor_ts = time.time()
        st.online = True
        st.last_heartbeat = st.cursor_ts
        self._emit(space_id, "cursor", st.to_dict())
        logger.debug(f"cursor space={space_id} user={user_id} ({x},{y}) target={target}")
        return st

    def list_present(self, space_id: str) -> List[PresenceState]:
        states = self._states.get(space_id, {})
        self._gc(states)
        return list(states.values())

    def get(self, space_id: str, user_id: str) -> Optional[PresenceState]:
        states = self._states.get(space_id)
        if not states:
            return None
        st = states.get(user_id)
        if st is None:
            return None
        if time.time() - st.last_heartbeat > self._timeout:
            st.online = False
        return st

    def remove(self, space_id: str, user_id: str) -> bool:
        states = self._states.get(space_id)
        if not states or user_id not in states:
            return False
        del states[user_id]
        self._emit(space_id, "presence", {"user_id": user_id, "online": False, "removed": True})
        logger.info(f"presence remove space={space_id} user={user_id}")
        return True

    def _gc(self, states: Dict[str, PresenceState]) -> None:
        # R-6: 旧版仅置 online=False 不删除 → ghost presence 永驻 (内存泄漏 + 假在线)。
        # 超时直接删除条目, 像离开一样清理。
        now = time.time()
        expired = [uid for uid, st in states.items() if now - st.last_heartbeat > self._timeout]
        for uid in expired:
            del states[uid]
            logger.debug(f"presence GC 过期移除 user={uid} (超 {self._timeout}s 未心跳)")
        return len(expired)

    def _emit(self, space_id: str, kind: str, data: Dict[str, Any]) -> None:
        if self._emitter is None:
            return
        try:
            self._emitter.create_event(
                event_type=f"space:{space_id}:{kind}",
                data=data,
            )
        except Exception as e:
            logger.warning(f"presence 事件推送失败: {e}")
