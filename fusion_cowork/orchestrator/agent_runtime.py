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

from .comm import AgentMessage, AgentMessageBus
from .orchestrator import Agent

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent 运行时 — 独立执行环境。"""

    _MAX_RESULTS = 256
    # R-2: 单任务执行超时上限 (秒), 防 executor 卡死永不终态
    _EXEC_TIMEOUT = 120.0

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
        # R-2: 在途 executor 协程 handle, 供 cancel/stop 真取消 (旧版仅翻 flag, executor 继续跑)
        self._exec_handle: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self.agent.status = "idle"
        self._task = asyncio.create_task(self._message_loop())
        logger.info(f"AgentRuntime 启动: {self.agent.name} ({self.agent.agent_id})")

    async def stop(self) -> None:
        self._running = False
        # R-2: 停机先杀在途 executor, 否则 _task 取消后 executor 仍悬挂
        if self._exec_handle and not self._exec_handle.done():
            self._exec_handle.cancel()
            try:
                await self._exec_handle
            except (asyncio.CancelledError, Exception):
                pass
            self._exec_handle = None
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
                    self.inbox.get(),
                    timeout=1.0,
                )
                await self._handle_message(msg)
            except TimeoutError:
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
            # R-2: 取消在途 executor 协程, 非仅翻 flag (旧版 executor 继续跑成僵尸)
            if self._exec_handle and not self._exec_handle.done():
                self._exec_handle.cancel()
                logger.info(f"Agent {self.agent.agent_id} 在途任务协程已取消: {self._current_task_id}")
            self._exec_handle = None
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
                # R-2: wait_for 强制超时 + 存 handle 供 cancel 真取消 (旧 await executor 无超时无取消)
                self._exec_handle = asyncio.ensure_future(self.executor(task_input))
                result = await asyncio.wait_for(asyncio.shield(self._exec_handle), timeout=self._EXEC_TIMEOUT)
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
        except TimeoutError:
            # R-2: wait_for 超时, executor 协程仍在跑 → 显式取消
            if self._exec_handle and not self._exec_handle.done():
                self._exec_handle.cancel()
            self._results[task_id] = {"error": f"执行超时 ({self._EXEC_TIMEOUT}s)"}
            try:
                await self.message_bus.publish(
                    topic=f"task_result:{task_id}",
                    sender=self.agent.agent_id,
                    payload={
                        "task_id": task_id,
                        "status": "failed",
                        "error": f"执行超时 ({self._EXEC_TIMEOUT}s)",
                        "elapsed": time.time() - start_time,
                    },
                )
            except Exception as pub_err:
                logger.error(f"Agent {self.agent.agent_id} 发布超时消息异常: {pub_err}")
            logger.warning(f"Agent {self.agent.agent_id} 执行超时: {task_id} ({self._EXEC_TIMEOUT}s)")
        except asyncio.CancelledError:
            # R-2: 外部 cancel (stop/cancel action) → 走终态, 不当 failed
            self._results[task_id] = {"error": "已取消"}
            logger.info(f"Agent {self.agent.agent_id} 任务被取消: {task_id}")
            raise
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
            self._exec_handle = None

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
