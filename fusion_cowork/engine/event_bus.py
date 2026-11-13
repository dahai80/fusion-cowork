"""Process-wide inbound event bus (issue #459).

wait_for_event nodes await named events; inbound trigger hooks (and any other
producer, e.g. POST /api/events/{name}) emit them. In-memory by design for
phase 1 — events are wake signals, not a queue; retained history is a later
workstream (durable event waits, plan item 2.x). Singleton access mirrors the
guard-client pattern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_bus: EventBus | None = None


class EventBus:
    def __init__(self) -> None:
        self._waiters: Dict[str, List[asyncio.Future]] = {}

    async def wait(self, name: str, timeout: float = 300.0) -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("event name 必填")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.setdefault(name, []).append(fut)
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        finally:
            waiters = self._waiters.get(name)
            if waiters and fut in waiters:
                waiters.remove(fut)
                if not waiters:
                    self._waiters.pop(name, None)

    def emit(self, name: str, payload: Any = None) -> int:
        name = str(name or "").strip()
        if not name:
            raise ValueError("event name 必填")
        waiters = self._waiters.pop(name, None)
        if not waiters:
            logger.info(f"event emitted with no waiters: {name}")
            return 0
        woken = 0
        for fut in waiters:
            if not fut.done():
                fut.set_result({"event": name, "payload": payload if isinstance(payload, dict) else {}})
                woken += 1
        logger.info(f"event emitted: {name} woke {woken} waiter(s)")
        return woken

    def pending_names(self) -> List[str]:
        return [n for n, ws in self._waiters.items() if ws]


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
