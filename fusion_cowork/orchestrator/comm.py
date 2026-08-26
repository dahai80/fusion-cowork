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
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List

logger = logging.getLogger(__name__)

_QUEUE_MAX = 1024
_HISTORY_MAX = 1000
# E-6: 丢弃消息保留上限, 供 get_dropped() 回查 (审计: 静默丢无重投无回传)
_DROPPED_MAX = 256


class MessageDeliveryError(RuntimeError):
    """E-6: 点对点投递全部失败 — 调用方须感知, 不再拿 msg_id 当成功。"""

    def __init__(self, sender: str, receiver: str, msg_id: str, reason: str = "队列满"):
        self.sender = sender
        self.receiver = receiver
        self.msg_id = msg_id
        super().__init__(f"消息投递失败 {sender}→{receiver} msg_id={msg_id}: {reason}")


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
        self._history: Deque[AgentMessage] = deque(maxlen=_HISTORY_MAX)
        # E-6: 丢弃消息留底 (点对点 raise 前也存, 供诊断/重投)
        self._dropped: Deque[AgentMessage] = deque(maxlen=_DROPPED_MAX)

    def subscribe(self, topic: str) -> asyncio.Queue:
        """订阅主题，返回消息队列。"""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
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
        dropped = 0
        for q in queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dropped += 1
                # E-6: 广播丢弃升 ERROR (旧版仅 warning 静默吞), 留底供 get_dropped() 回查
                logger.error(f"消息总线: 队列满 topic={topic} 丢弃 msg_id={msg.msg_id} (订阅者未消费)")
        if dropped > 0:
            self._dropped.append(msg)

        logger.info(f"消息发布: {sender} → {topic} ({len(queues)} 订阅者, 丢弃 {dropped})")
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

        delivered = 0
        full_queues = 0
        for q in all_queues:
            try:
                q.put_nowait(msg)
                delivered += 1
            except asyncio.QueueFull:
                full_queues += 1
                logger.error(f"消息总线: inbox 满 receiver={receiver} msg_id={msg.msg_id} (可能死 agent, 队列未消费)")

        logger.info(f"点对点: {sender} → {receiver} (投递 {delivered}/{len(all_queues)})")
        # E-6: 全部投递失败 → raise, 调用方拿到异常不再误判 msg_id=成功 (审计: 静默丢无错误回传)
        if delivered == 0 and all_queues:
            self._dropped.append(msg)
            raise MessageDeliveryError(
                sender=sender, receiver=receiver, msg_id=msg.msg_id, reason=f"{len(all_queues)} 队列均满"
            )
        return msg.msg_id

    def get_history(self, topic: str = "", limit: int = 100) -> List[AgentMessage]:
        """获取消息历史。"""
        msgs = list(self._history)
        if topic:
            msgs = [m for m in msgs if m.topic == topic or m.topic.startswith(topic)]
        return msgs[-limit:]

    def clear_history(self) -> None:
        """清除消息历史。"""
        self._history.clear()

    def get_dropped(self, limit: int = 100) -> List[AgentMessage]:
        """E-6: 取最近丢弃消息 (供诊断/重投)。"""
        return list(self._dropped)[-limit:]

    def clear_dropped(self) -> None:
        """E-6: 清除丢弃消息留底。"""
        self._dropped.clear()
