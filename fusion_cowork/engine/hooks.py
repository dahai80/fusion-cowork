"""Hook 系统 — 14 种 Hook 事件 (对标 Claude Cowork)。

节点执行前/后、工作流开始/结束、权限请求、配置变更、智能体启停、会话起止、
压缩前、通知、节点错误、工作流取消等事件可被自定义处理器拦截。
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
    """Hook 管理器 — 注册/触发事件处理器。"""

    def __init__(self):
        self._handlers: Dict[HookEvent, List[tuple]] = {}
        # R-3: _handlers 并发保护 — register/unregister/clear 与 fire 遍历竞态。
        self._lock = threading.Lock()

    def register(self, event: HookEvent, handler: Callable, priority: int = 0) -> None:
        with self._lock:
            if event not in self._handlers:
                self._handlers[event] = []
            self._handlers[event].append((priority, handler))
            self._handlers[event].sort(key=lambda x: x[0], reverse=True)
        logger.info(f"Hook 注册: {event.value} → {getattr(handler, '__name__', str(handler))} (priority={priority})")

    def unregister(self, event: HookEvent, handler: Callable) -> None:
        with self._lock:
            if event in self._handlers:
                self._handlers[event] = [(p, h) for p, h in self._handlers[event] if h is not handler]

    async def fire(self, event: HookEvent, data: Dict[str, Any] = None) -> HookContext:
        ctx = HookContext(event=event, data=data or {})

        # R-3: 拷贝 handlers 列表 (勿返引用) — fire 期间 register/unregister 不影响本轮, 也无竞态。
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        if not handlers:
            return ctx

        # E-14: PRE_* 验证类事件 — handler 抛异常 = 校验器故障, fail-closed 取消 (勿静默跳过)。
        fail_closed = event in (HookEvent.PRE_NODE_EXECUTE, HookEvent.PERMISSION_REQUEST, HookEvent.PRE_COMPACT)
        loop = asyncio.get_event_loop()

        for priority, handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(ctx)
                else:
                    # E-14: 同步 handler 走 run_in_executor, 勿阻塞事件循环 (原 handler(ctx) 直跑)。
                    await loop.run_in_executor(None, handler, ctx)
                if ctx.cancelled:
                    logger.info(f"Hook 取消: {event.value} (by {getattr(handler, '__name__', '?')})")
                    break
            except Exception as e:
                logger.error(f"Hook 处理异常: {event.value} → {e}", exc_info=True)
                if fail_closed:
                    logger.warning(f"Hook fail-closed: {event.value} handler 异常, 取消事件流")
                    ctx.cancel()
                    break

        return ctx

    def get_registered_events(self) -> List[str]:
        with self._lock:
            return [e.value for e in self._handlers if self._handlers[e]]

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
