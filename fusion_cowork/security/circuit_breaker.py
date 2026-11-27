"""Stage 6 - upstream circuit breaker (CircuitBreaker CLOSED/OPEN/HALF_OPEN).

Protects upstreams such as MLX/KB: consecutive failures reaching threshold -> OPEN, calls rejected (downstream not hung); after recovery_timeout -> HALF_OPEN
admits one probe, success -> CLOSED, failure -> OPEN again.

async wrapper call(): wraps coroutines; raises CircuitOpenError when open (caller catches and degrades).
sync wrapper call_sync(): wraps plain functions.
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
    """Circuit open - call rejected, caller should degrade."""


class CircuitBreaker:
    """Three-state circuit breaker - per-name instance (e.g. "mlx", "kb").

    threshold: consecutive failures reaching this value -> OPEN
    recovery_timeout: seconds after OPEN -> HALF_OPEN
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
                logger.info(f"circuit[{self.name}] OPEN->HALF_OPEN (recovery_timeout elapsed)")
        return self._state

    def is_open(self) -> bool:
        return self.state in (CircuitState.OPEN,)

    def is_call_allowed(self) -> bool:
        return self.state != CircuitState.OPEN

    def on_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info(f"circuit[{self.name}] {self._state.value}->CLOSED (success)")
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
            logger.warning(f"circuit[{self.name}] HALF_OPEN->OPEN (probe failed)")
            self._trip()
            return
        if self._failures >= self.failure_threshold:
            logger.warning(f"circuit[{self.name}] CLOSED->OPEN (failures {self._failures}/{self.failure_threshold})")
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
        """Wrap an async call; OPEN -> raises CircuitOpenError."""
        if not self.is_call_allowed():
            logger.warning(f"circuit[{self.name}] OPEN rejecting call (degrade)")
            raise CircuitOpenError(f"circuit[{self.name}] open")
        try:
            result = await fn(*args, **kwargs)
        except CircuitOpenError:
            raise
        except Exception as e:
            logger.debug(f"circuit[{self.name}] call failed: {e}")
            self.on_failure()
            raise
        self.on_success()
        return result

    def call_sync(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Wrap a sync call; OPEN -> raises CircuitOpenError."""
        if not self.is_call_allowed():
            logger.warning(f"circuit[{self.name}] OPEN rejecting call (degrade)")
            raise CircuitOpenError(f"circuit[{self.name}] open")
        try:
            result = fn(*args, **kwargs)
        except CircuitOpenError:
            raise
        except Exception as e:
            logger.debug(f"circuit[{self.name}] call failed: {e}")
            self.on_failure()
            raise
        self.on_success()
        return result
