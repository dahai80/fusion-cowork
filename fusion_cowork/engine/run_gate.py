"""Run-level concurrency gate (issue #461).

Keyed admission control for workflow runs: cap concurrent RUNNING runs per
concurrency_key so runs with distinct keys interleave (fair share) while
same-key runs queue FIFO. An optional global cap sits on top. Opt-in via env —
both caps unset/0 means no gating and zero behavior change.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        logger.warning(f"{name} invalid: {raw!r}, using {default}")
        return default


class RunGate:
    def __init__(self, max_running: int = 0, max_per_key: int = 0):
        self._max_running = max(0, int(max_running))
        self._max_per_key = max(0, int(max_per_key))
        self._global_sem = asyncio.Semaphore(self._max_running) if self._max_running > 0 else None
        self._key_sems: Dict[str, asyncio.Semaphore] = {}
        self._running_by_key: Dict[str, int] = {}

    @classmethod
    def from_env(cls) -> RunGate:
        return cls(
            max_running=_int_env("FUSION_COWORK_MAX_RUNNING_RUNS", 0),
            max_per_key=_int_env("FUSION_COWORK_MAX_RUNNING_PER_KEY", 0),
        )

    @property
    def enabled(self) -> bool:
        return self._max_running > 0 or self._max_per_key > 0

    def saturated(self, key: str) -> bool:
        # non-async admission probe — lets start() mark a run "queued" upfront
        if self._global_sem is not None and self._global_sem.locked():
            return True
        sem = self._key_sems.get(key)
        return sem is not None and sem.locked()

    async def acquire(self, key: str) -> None:
        if self._max_per_key > 0:
            sem = self._key_sems.get(key)
            if sem is None:
                sem = asyncio.Semaphore(self._max_per_key)
                self._key_sems[key] = sem
            await sem.acquire()
        if self._global_sem is not None:
            await self._global_sem.acquire()
        self._running_by_key[key] = self._running_by_key.get(key, 0) + 1
        logger.debug(f"run gate acquired key={key} running={dict(self._running_by_key)}")

    def release(self, key: str) -> None:
        self._running_by_key[key] = max(0, self._running_by_key.get(key, 0) - 1)
        if self._global_sem is not None:
            self._global_sem.release()
        sem = self._key_sems.get(key)
        if sem is not None:
            sem.release()
        logger.debug(f"run gate released key={key} running={dict(self._running_by_key)}")

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_running": self._max_running,
            "max_per_key": self._max_per_key,
            "running_by_key": dict(self._running_by_key),
        }
