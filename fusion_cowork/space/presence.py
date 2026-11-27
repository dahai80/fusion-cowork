"""Member presence + realtime cursors — collaboration space realtime state layer.

Presence: member online/offline state, determined by heartbeat timestamps (timeout threshold defaults to 60s).
Cursor: member realtime cursor position (x, y, target_id), in-memory only, not persisted.

Events are pushed via EventEmitter: space:{id}:presence, space:{id}:cursor.
Degradation: when EventEmitter is not injected, only in-memory state is maintained, no push (silent).
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
        logger.debug(f"PresenceManager initialized, timeout={timeout}s")

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
        # R-6: the old version only set online=False without deleting → ghost presence persisted forever (memory leak + fake online).
        # Expired entries are deleted directly on timeout, cleaned up as if the user left.
        now = time.time()
        expired = [uid for uid, st in states.items() if now - st.last_heartbeat > self._timeout]
        for uid in expired:
            del states[uid]
            logger.debug(f"presence GC expired removal user={uid} (no heartbeat for over {self._timeout}s)")
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
            logger.warning(f"presence event push failed: {e}")
