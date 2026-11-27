"""Agent runtime — an isolated execution environment for a single agent.

Each AgentRuntime wraps one agent and provides:
- Message loop: receives and handles messages from the inbox
- Executor invocation: hands tasks to the registered executor
- Lifecycle management: start/stop
- Error recovery: notifies the message bus on execution failure
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
    """Agent runtime — isolated execution environment."""

    _MAX_RESULTS = 256
    # R-2: per-task execution timeout cap (seconds), prevents a stuck executor from never reaching a terminal state
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
        # R-2: handle of the in-flight executor coroutine, so cancel/stop truly cancels (old version only flipped a flag, executor kept running)
        self._exec_handle: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self.agent.status = "idle"
        self._task = asyncio.create_task(self._message_loop())
        logger.info(f"AgentRuntime started: {self.agent.name} ({self.agent.agent_id})")

    async def stop(self) -> None:
        self._running = False
        # R-2: on shutdown kill the in-flight executor first, otherwise the executor stays hung after _task cancel
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
        logger.info(f"AgentRuntime stopped: {self.agent.name}")

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
                logger.error(f"AgentRuntime {self.agent.agent_id} message loop error: {e}")

    async def _handle_message(self, msg: AgentMessage) -> None:
        payload = msg.payload
        action = payload.get("action", "")

        if action == "execute":
            await self._execute_task(msg)
        elif action == "cancel":
            # R-2: cancel the in-flight executor coroutine, not just flip a flag (old version left the executor running as a zombie)
            if self._exec_handle and not self._exec_handle.done():
                self._exec_handle.cancel()
                logger.info(f"Agent {self.agent.agent_id} in-flight task coroutine cancelled: {self._current_task_id}")
            self._exec_handle = None
            self._current_task_id = ""
            self.agent.status = "idle"
            logger.info(f"Agent {self.agent.agent_id} task cancelled")
        elif action == "ping":
            await self.message_bus.publish(
                topic=f"pong:{self.agent.agent_id}",
                sender=self.agent.agent_id,
                payload={"status": self.agent.status},
            )
        else:
            logger.debug(f"Agent {self.agent.agent_id} received unknown action: {action}")

    async def _execute_task(self, msg: AgentMessage) -> None:
        task_input = msg.payload.get("input_data", {})
        task_id = msg.payload.get("task_id", "unknown")
        self._current_task_id = task_id
        self.agent.status = "busy"
        self.agent.current_task = task_id
        start_time = time.time()

        try:
            if asyncio.iscoroutinefunction(self.executor):
                # R-2: wait_for enforces timeout + store handle for true cancel (old plain await executor had no timeout and no cancel)
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
            logger.info(f"Agent {self.agent.agent_id} completed: {task_id}")
        except TimeoutError:
            # R-2: wait_for timed out, executor coroutine still running → cancel it explicitly
            if self._exec_handle and not self._exec_handle.done():
                self._exec_handle.cancel()
            self._results[task_id] = {"error": f"execution timed out ({self._EXEC_TIMEOUT}s)"}
            try:
                await self.message_bus.publish(
                    topic=f"task_result:{task_id}",
                    sender=self.agent.agent_id,
                    payload={
                        "task_id": task_id,
                        "status": "failed",
                        "error": f"execution timed out ({self._EXEC_TIMEOUT}s)",
                        "elapsed": time.time() - start_time,
                    },
                )
            except Exception as pub_err:
                logger.error(f"Agent {self.agent.agent_id} failed to publish timeout message: {pub_err}")
            logger.warning(f"Agent {self.agent.agent_id} execution timed out: {task_id} ({self._EXEC_TIMEOUT}s)")
        except asyncio.CancelledError:
            # R-2: external cancel (stop/cancel action) → go to terminal state, not failed
            self._results[task_id] = {"error": "cancelled"}
            logger.info(f"Agent {self.agent.agent_id} task cancelled: {task_id}")
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
                logger.error(f"Agent {self.agent.agent_id} failed to publish failure message: {pub_err}")

            logger.error(f"Agent {self.agent.agent_id} execution failed: {task_id} → {e}")
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
