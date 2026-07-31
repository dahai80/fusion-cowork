"""Agent 通信总线 — Agent 间消息传递与事件广播。

支持:
- 点对点消息 (send)
- 主题广播 (publish/subscribe)
- 任务状态事件
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    """Agent 间消息。"""
    msg_id: str
    sender: str
    receiver: str  # "*" = 广播
    topic: str
    payload: Dict[str, Any]
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = f"msg_{uuid.uuid4().hex[:8]}"
        if not self.timestamp:
            self.timestamp = time.time()


class AgentMessageBus:
    """Agent 消息总线 — 发布/订阅模式。"""

    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._history: List[AgentMessage] = []

    def subscribe(self, topic: str) -> asyncio.Queue:
        """订阅主题，返回消息队列。"""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[topic].append(q)
        logger.info(f"消息总线: 订阅 {topic}")
        return q

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """取消订阅。"""
        if topic in self._subscribers:
            self._subscribers[topic] = [q for q in self._subscribers[topic] if q is not queue]

    async def publish(self, topic: str, sender: str, payload: Dict[str, Any]) -> str:
        """发布消息到主题。"""
        msg = AgentMessage(
            msg_id=f"msg_{uuid.uuid4().hex[:8]}",
            sender=sender,
            receiver="*",
            topic=topic,
            payload=payload,
        )
        self._history.append(msg)

        queues = self._subscribers.get(topic, [])
        for q in queues:
            await q.put(msg)

        logger.info(f"消息发布: {sender} → {topic} ({len(queues)} 订阅者)")
        return msg.msg_id

    async def send(self, sender: str, receiver: str, payload: Dict[str, Any]) -> str:
        """点对点发送消息。"""
        msg = AgentMessage(
            msg_id=f"msg_{uuid.uuid4().hex[:8]}",
            sender=sender,
            receiver=receiver,
            topic=f"direct:{sender}:{receiver}",
            payload=payload,
        )
        self._history.append(msg)

        queues = self._subscribers.get(f"direct:{sender}:{receiver}", [])
        inbox_queues = self._subscribers.get(f"inbox:{receiver}", [])
        all_queues = queues + inbox_queues

        for q in all_queues:
            await q.put(msg)

        logger.info(f"点对点: {sender} → {receiver}")
        return msg.msg_id

    def get_history(self, topic: str = "", limit: int = 100) -> List[AgentMessage]:
        """获取消息历史。"""
        msgs = self._history
        if topic:
            msgs = [m for m in msgs if m.topic == topic or m.topic.startswith(topic)]
        return msgs[-limit:]

    def clear_history(self) -> None:
        """清除消息历史。"""
        self._history.clear()
