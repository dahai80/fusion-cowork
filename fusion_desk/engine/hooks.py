"""Hook 系统 — 对标 Claude Cowork 的 11 种 Hook。

节点执行前/后、工作流开始/结束、权限请求、配置变更等事件可被自定义处理器拦截。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

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
        self._handlers: Dict[HookEvent, List[Callable]] = {}

    def register(self, event: HookEvent, handler: Callable) -> None:
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)
        logger.info(f"Hook 注册: {event.value} → {getattr(handler, '__name__', str(handler))}")

    def unregister(self, event: HookEvent, handler: Callable) -> None:
        if event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def fire(self, event: HookEvent, data: Dict[str, Any] = None) -> HookContext:
        ctx = HookContext(event=event, data=data or {})

        handlers = self._handlers.get(event, [])
        if not handlers:
            return ctx

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(ctx)
                else:
                    handler(ctx)
                if ctx.cancelled:
                    logger.info(f"Hook 取消: {event.value} (by {getattr(handler, '__name__', '?')})")
                    break
            except Exception as e:
                logger.error(f"Hook 处理异常: {event.value} → {e}")

        return ctx

    def get_registered_events(self) -> List[str]:
        return [e.value for e in self._handlers if self._handlers[e]]

    def clear(self) -> None:
        self._handlers.clear()
