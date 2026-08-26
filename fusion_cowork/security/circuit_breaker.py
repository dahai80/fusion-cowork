"""Stage 6 — 上游熔断 (CircuitBreaker CLOSED/OPEN/HALF_OPEN)。

保护 MLX/KB 等上游: 连续失败达 threshold → OPEN, 拒调 (不挂下游); recovery_timeout 后 → HALF_OPEN
放一条试探, 成功 → CLOSED, 失败 → 重新 OPEN。

async 装饰器 call(): 包协程; open 时抛 CircuitOpenError (调用方捕后降级)。
sync 调用 call_sync(): 包普通函数。
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """熔断开启 — 调用被拒, 调用方应降级。"""


class CircuitBreaker:
    """三态熔断器 — per-name 实例 (如 "mlx", "kb")。

    threshold: 连续失败数达此值 → OPEN
    recovery_timeout: OPEN 后多少秒 → HALF_OPEN
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = max(1.0, float(recovery_timeout))
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._on_open: Optional[Callable[[str], None]] = None
        self._on_close: Optional[Callable[[str], None]] = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info(f"circuit[{self.name}] OPEN→HALF_OPEN (recovery_timeout 到)")
        return self._state

    def is_open(self) -> bool:
        return self.state in (CircuitState.OPEN,)

    def is_call_allowed(self) -> bool:
        return self.state != CircuitState.OPEN

    def on_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info(f"circuit[{self.name}] {self._state.value}→CLOSED (成功)")
            self._state = CircuitState.CLOSED
            if self._on_close:
                try:
                    self._on_close(self.name)
                except Exception:
                    pass
        self._failures = 0

    def on_failure(self) -> None:
        self._failures += 1
        if self._state == CircuitState.HALF_OPEN:
            logger.warning(f"circuit[{self.name}] HALF_OPEN→OPEN (试探失败)")
            self._trip()
            return
        if self._failures >= self.failure_threshold:
            logger.warning(f"circuit[{self.name}] CLOSED→OPEN (失败 {self._failures}/{self.failure_threshold})")
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        if self._on_open:
            try:
                self._on_open(self.name)
            except Exception:
                pass

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    async def call(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        """包协程调用; OPEN → 抛 CircuitOpenError。"""
        if not self.is_call_allowed():
            logger.warning(f"circuit[{self.name}] OPEN 拒调 (降级)")
            raise CircuitOpenError(f"circuit[{self.name}] open")
        try:
            result = await fn(*args, **kwargs)
        except CircuitOpenError:
            raise
        except Exception as e:
            logger.debug(f"circuit[{self.name}] 调用失败: {e}")
            self.on_failure()
            raise
        self.on_success()
        return result

    def call_sync(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """包同步调用; OPEN → 抛 CircuitOpenError。"""
        if not self.is_call_allowed():
            logger.warning(f"circuit[{self.name}] OPEN 拒调 (降级)")
            raise CircuitOpenError(f"circuit[{self.name}] open")
        try:
            result = fn(*args, **kwargs)
        except CircuitOpenError:
            raise
        except Exception as e:
            logger.debug(f"circuit[{self.name}] 调用失败: {e}")
            self.on_failure()
            raise
        self.on_success()
        return result
