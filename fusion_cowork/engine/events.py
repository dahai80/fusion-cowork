"""流式事件 — 工作流执行过程的实时事件推送。

EventEmitter 支持：
- 事件发布/订阅（pub/sub）
- SSE 推送到 HTTP 客户端
- 事件缓冲（断线重连后回放）
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    WORKFLOW_CANCEL = "workflow_cancel"
    NODE_START = "node_start"
    NODE_END = "node_end"
    NODE_ERROR = "node_error"
    NODE_DENIED = "node_denied"
    PROGRESS = "progress"
    LOG = "log"
    PERMISSION_REQUEST = "permission_request"


@dataclass
class WorkflowEvent:
    event_id: str = ""
    event_type: str = ""
    execution_id: str = ""
    node_id: str = ""
    node_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.event_id:
            self.event_id = f"evt_{uuid.uuid4().hex[:8]}"
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_sse(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False)
        return f"event: {self.event_type}\ndata: {payload}\n\n"


class EventEmitter:
    def __init__(self, buffer_size: int = 200):
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._buffer: List[WorkflowEvent] = []
        self._buffer_size = buffer_size
        self._callbacks: List[Callable] = []

    def subscribe(self, sub_id: Optional[str] = None) -> tuple:
        sub_id = sub_id or f"sub_{uuid.uuid4().hex[:6]}"
        queue = asyncio.Queue(maxsize=500)
        self._subscribers[sub_id] = queue
        logger.debug(f"EventEmitter 订阅: {sub_id}")
        return sub_id, queue

    def unsubscribe(self, sub_id: str):
        self._subscribers.pop(sub_id, None)
        logger.debug(f"EventEmitter 取消订阅: {sub_id}")

    def on_event(self, callback: Callable):
        self._callbacks.append(callback)

    def emit(self, event: WorkflowEvent):
        self._buffer.append(event)
        if len(self._buffer) > self._buffer_size:
            self._buffer = self._buffer[-self._buffer_size :]
        for sub_id, queue in self._subscribers.items():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"订阅 {sub_id} 队列已满，丢弃事件")
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"事件回调异常: {e}")

    def get_buffered(self, since: float = 0.0) -> List[WorkflowEvent]:
        if since <= 0:
            return list(self._buffer)
        return [e for e in self._buffer if e.timestamp > since]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def create_event(
        self,
        event_type: str,
        execution_id: str = "",
        node_id: str = "",
        node_name: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> WorkflowEvent:
        event = WorkflowEvent(
            event_type=event_type,
            execution_id=execution_id,
            node_id=node_id,
            node_name=node_name,
            data=data or {},
        )
        self.emit(event)
        return event
