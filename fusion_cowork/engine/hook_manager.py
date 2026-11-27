"""Hook system — 14 hook event types (parity with Claude Cowork).

Pre/post node execution, workflow start/end, permission request, config change,
agent start/stop, session start/end, pre-compact, notification, node error,
workflow cancel — custom handlers can intercept these events.

#521 drop-fallback ruling (2026-11-18): this is the primary cowork hook
implementation, NOT an in-tree fallback copy. fusion-harness is a mandatory
install (unified venv, [plugins] extra) — missing harness is a startup
error via the shims (hooks.py), never a degraded mode. Cowork hook semantics
(priority ordering, HookContext mutation, fail-closed PRE_*) do not map 1:1
onto the kernel, so this file stays the product implementation; kernel
hook types (HookEngine/HookConfig/HookResult) come in through hooks.py.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class HookEvent(Enum):
    PRE_NODE_EXECUTE = "pre_node_execute"
    POST_NODE_EXECUTE = "post_node_execute"
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    PERMISSION_REQUEST = "permission_request"
    CONFIG_CHANGE = "config_change"
    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    NOTIFICATION = "notification"
    NODE_ERROR = "node_error"
    WORKFLOW_CANCEL = "workflow_cancel"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_COMPACT = "pre_compact"


@dataclass
class HookContext:
    event: HookEvent
    data: Dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False
    modified_data: Dict[str, Any] = field(default_factory=dict)

    def cancel(self) -> None:
        self.cancelled = True

    def modify(self, key: str, value: Any) -> None:
        self.modified_data[key] = value


class HookManager:
    """Hook manager — register/fire event handlers."""

    def __init__(self):
        self._handlers: Dict[HookEvent, List[tuple]] = {}
        # R-3: concurrency protection for _handlers — register/unregister/clear vs fire traversal race.
        self._lock = threading.Lock()

    def register(self, event: HookEvent, handler: Callable, priority: int = 0) -> None:
        with self._lock:
            if event not in self._handlers:
                self._handlers[event] = []
            self._handlers[event].append((priority, handler))
            self._handlers[event].sort(key=lambda x: x[0], reverse=True)
        logger.info(
            f"Hook registered: {event.value} → {getattr(handler, '__name__', str(handler))} (priority={priority})"
        )

    def unregister(self, event: HookEvent, handler: Callable) -> None:
        with self._lock:
            if event in self._handlers:
                self._handlers[event] = [(p, h) for p, h in self._handlers[event] if h is not handler]

    async def fire(self, event: HookEvent, data: Dict[str, Any] = None) -> HookContext:
        ctx = HookContext(event=event, data=data or {})

        # R-3: copy the handlers list (never return a reference) — register/unregister during fire does not affect this round, and there is no race.
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        if not handlers:
            return ctx

        # E-14: PRE_* validation events — a handler raising = validator failure, fail-closed cancels (never silently skip).
        fail_closed = event in (HookEvent.PRE_NODE_EXECUTE, HookEvent.PERMISSION_REQUEST, HookEvent.PRE_COMPACT)
        loop = asyncio.get_event_loop()

        for priority, handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(ctx)
                else:
                    # E-14: sync handlers go through run_in_executor, never block the event loop (previously handler(ctx) ran inline).
                    await loop.run_in_executor(None, handler, ctx)
                if ctx.cancelled:
                    logger.info(f"Hook cancel: {event.value} (by {getattr(handler, '__name__', '?')})")
                    break
            except Exception as e:
                logger.error(f"Hook handler error: {event.value} → {e}", exc_info=True)
                if fail_closed:
                    logger.warning(f"Hook fail-closed: {event.value} handler raised, canceling event stream")
                    ctx.cancel()
                    break

        return ctx

    def get_registered_events(self) -> List[str]:
        with self._lock:
            return [e.value for e in self._handlers if self._handlers[e]]

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
