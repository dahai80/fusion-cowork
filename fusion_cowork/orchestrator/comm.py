"""Agent communication bus — inter-agent message passing and event broadcasting.

Supports:
- Point-to-point messages (send)
- Topic broadcast (publish/subscribe)
- Task status events

#533 K4 Phase B item-1 ruling (2026-11-19): kernel-verified DIVERGENT-BY-DESIGN.
This bus is intentionally NOT mapped onto kernel TeamRuntime.send/inbox:
- kernel mailbox is member-scoped — sender/recipient/mentions must exist in
  the TeamDefinition, message type is a closed set (MSG_TYPES = text,
  plan_approval, shutdown_request, shutdown_response, destructive_gate), body is a plain str,
  delivery is a durable state_dir file write with a {"delivered": bool} flag;
- cowork bus is an open in-process transport: arbitrary topic strings,
  arbitrary dict payloads, publisher need not be a team member (orchestrator
  executors/coordinator publish lifecycle events), "*" broadcast fan-out,
  asyncio.Queue semantics with E-6 MessageDeliveryError + dropped-message
  audit (get_dropped).
Mapping would force non-member system components through member validation
and flatten structured payloads to string bodies — product semantics lost.
Kernel TeamRuntime remains the substrate for space team runs (space plane,
#496) and task lifecycle convergence (#533 item 4); this bus stays the DAG
orchestrator's internal transport.
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
# E-6: dropped-message retention cap, for get_dropped() inspection (audit: silent drop with no redelivery or echo)
_DROPPED_MAX = 256


class MessageDeliveryError(RuntimeError):
    """E-6: all point-to-point deliveries failed — callers must know; msg_id no longer masquerades as success."""

    # i18n: user-facing
    def __init__(self, sender: str, receiver: str, msg_id: str, reason: str = "队列满"):
        self.sender = sender
        self.receiver = receiver
        self.msg_id = msg_id
        # i18n: user-facing
        super().__init__(f"消息投递失败 {sender}→{receiver} msg_id={msg_id}: {reason}")


@dataclass
class AgentMessage:
    """Message between agents."""

    msg_id: str
    sender: str
    receiver: str  # "*" = broadcast
    topic: str
    payload: Dict[str, Any]
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = f"msg_{uuid.uuid4().hex[:8]}"
        if not self.timestamp:
            self.timestamp = time.time()


class AgentMessageBus:
    """Agent message bus — publish/subscribe."""

    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._history: Deque[AgentMessage] = deque(maxlen=_HISTORY_MAX)
        # E-6: keep dropped messages on record (also stored before point-to-point raise, for diagnosis/redelivery)
        self._dropped: Deque[AgentMessage] = deque(maxlen=_DROPPED_MAX)

    def subscribe(self, topic: str) -> asyncio.Queue:
        """Subscribe to a topic; returns the message queue."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers[topic].append(q)
        logger.info(f"Message bus: subscribed {topic}")
        return q

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """Unsubscribe."""
        if topic in self._subscribers:
            self._subscribers[topic] = [q for q in self._subscribers[topic] if q is not queue]

    async def publish(self, topic: str, sender: str, payload: Dict[str, Any]) -> str:
        """Publish a message to a topic."""
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
                # E-6: broadcast drop escalated to ERROR (old version silently swallowed at warning), recorded for get_dropped() inspection
                logger.error(
                    f"Message bus: queue full topic={topic} dropped msg_id={msg.msg_id} (subscriber did not consume)"
                )
        if dropped > 0:
            self._dropped.append(msg)

        # P2-9 (audit 0912): per-publish INFO flooded logs under concurrent
        # agents — demoted to DEBUG; drops stay at ERROR above.
        logger.debug(f"Message published: {sender} -> {topic} ({len(queues)} subscribers, dropped {dropped})")
        return msg.msg_id

    async def send(self, sender: str, receiver: str, payload: Dict[str, Any]) -> str:
        """Send a message point-to-point."""
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
                logger.error(
                    f"Message bus: inbox full receiver={receiver} msg_id={msg.msg_id} (possibly dead agent, queue unconsumed)"
                )

        logger.debug(f"Point-to-point: {sender} -> {receiver} (delivered {delivered}/{len(all_queues)})")
        # E-6: all deliveries failed -> raise, so callers no longer misread msg_id as success (audit: silent drop with no error echo)
        if delivered == 0 and all_queues:
            self._dropped.append(msg)
            raise MessageDeliveryError(
                sender=sender,
                receiver=receiver,
                msg_id=msg.msg_id,
                # i18n: user-facing
                reason=f"{len(all_queues)} 队列均满",
            )
        return msg.msg_id

    def get_history(self, topic: str = "", limit: int = 100) -> List[AgentMessage]:
        """Get message history."""
        msgs = list(self._history)
        if topic:
            msgs = [m for m in msgs if m.topic == topic or m.topic.startswith(topic)]
        return msgs[-limit:]

    def clear_history(self) -> None:
        """Clear message history."""
        self._history.clear()

    def get_dropped(self, limit: int = 100) -> List[AgentMessage]:
        """E-6: get recent dropped messages (for diagnosis/redelivery)."""
        return list(self._dropped)[-limit:]

    def clear_dropped(self) -> None:
        """E-6: clear the dropped-message record."""
        self._dropped.clear()
