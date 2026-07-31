"""Agent 运行时 — 单个 Agent 的独立执行环境。

每个 AgentRuntime 封装一个 Agent，提供:
- 消息循环: 从 inbox 接收消息并处理
- 执行器调用: 将任务交给注册的 executor
- 生命周期管理: start/stop
- 错误恢复: 执行失败时通知 message bus
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from .comm import AgentMessageBus, AgentMessage
from .orchestrator import Agent

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent 运行时 — 独立执行环境。"""

    _MAX_RESULTS = 256

    def __init__(
        self,
        agent: Agent,
        executor: Callable,
        message_bus: AgentMessageBus,
    ):
        self.agent = agent
        self.executor = executor
        self.message_bus = message_bus
        self.inbox: asyncio.Queue = message_bus.subscribe(f"inbox:{agent.agent_id}")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._current_task_id: str = ""
        self._results: Dict[str, Dict[str, Any]] = {}

    async def start(self) -> None:
        self._running = True
        self.agent.status = "idle"
        self._task = asyncio.create_task(self._message_loop())
        logger.info(f"AgentRuntime 启动: {self.agent.name} ({self.agent.agent_id})")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.agent.status = "idle"
        logger.info(f"AgentRuntime 停止: {self.agent.name}")

    async def _message_loop(self) -> None:
        while self._running:
            try:
                msg: AgentMessage = await asyncio.wait_for(
                    self.inbox.get(), timeout=1.0,
                )
                await self._handle_message(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AgentRuntime {self.agent.agent_id} 消息循环异常: {e}")

    async def _handle_message(self, msg: AgentMessage) -> None:
        payload = msg.payload
        action = payload.get("action", "")

        if action == "execute":
            await self._execute_task(msg)
        elif action == "cancel":
            self._current_task_id = ""
            self.agent.status = "idle"
            logger.info(f"Agent {self.agent.agent_id} 任务已取消")
        elif action == "ping":
            await self.message_bus.publish(
                topic=f"pong:{self.agent.agent_id}",
                sender=self.agent.agent_id,
                payload={"status": self.agent.status},
            )
        else:
            logger.debug(f"Agent {self.agent.agent_id} 收到未知 action: {action}")

    async def _execute_task(self, msg: AgentMessage) -> None:
        task_input = msg.payload.get("input_data", {})
        task_id = msg.payload.get("task_id", "unknown")
        self._current_task_id = task_id
        self.agent.status = "busy"
        self.agent.current_task = task_id
        start_time = time.time()

        try:
            if asyncio.iscoroutinefunction(self.executor):
                result = await self.executor(task_input)
            else:
                result = self.executor(task_input)

            output = result if isinstance(result, dict) else {"result": result}
            self._results[task_id] = output
            if len(self._results) > self._MAX_RESULTS:
                oldest = next(iter(self._results))
                del self._results[oldest]

            await self.message_bus.publish(
                topic=f"task_result:{task_id}",
                sender=self.agent.agent_id,
                payload={
                    "task_id": task_id,
                    "status": "completed",
                    "output": output,
                    "elapsed": time.time() - start_time,
                },
            )
            logger.info(f"Agent {self.agent.agent_id} 完成: {task_id}")
        except Exception as e:
            self._results[task_id] = {"error": str(e)}

            try:
                await self.message_bus.publish(
                    topic=f"task_result:{task_id}",
                    sender=self.agent.agent_id,
                    payload={
                        "task_id": task_id,
                        "status": "failed",
                        "error": str(e),
                        "elapsed": time.time() - start_time,
                    },
                )
            except Exception as pub_err:
                logger.error(f"Agent {self.agent.agent_id} 发布失败消息异常: {pub_err}")

            logger.error(f"Agent {self.agent.agent_id} 执行失败: {task_id} → {e}")
        finally:
            self.agent.status = "idle"
            self.agent.current_task = ""
            self._current_task_id = ""

    async def submit(self, task_id: str, input_data: Dict[str, Any]) -> None:
        await self.message_bus.send(
            sender="runtime",
            receiver=self.agent.agent_id,
            payload={
                "action": "execute",
                "task_id": task_id,
                "input_data": input_data,
            },
        )

    def get_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._results.get(task_id)

    @property
    def is_running(self) -> bool:
        return self._running
