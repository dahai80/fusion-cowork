"""Run-level concurrency gate (issue #461).

Keyed admission control for workflow runs: cap concurrent RUNNING runs per
concurrency_key so runs with distinct keys interleave (fair share) while
same-key runs queue FIFO. An optional global cap sits on top. Opt-in via env —
both caps unset/0 means no gating and zero behavior change.

issue #594: admission-time reservation — the slot is taken synchronously in
runs_start (try_reserve) so a same-key run is always `queued` when an earlier
same-key run has been admitted, even if that run's _execute has not reached
the gate yet. Slot accounting is plain counters (atomic on the single event
loop, no await between probe and reserve); queued runs wait via
acquire_reserved's poll loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.05


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
        self._global_running = 0
        self._key_running: Dict[str, int] = {}

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
        # pure synchronous probe — no await between this and try_reserve
        if self._max_running > 0 and self._global_running >= self._max_running:
            return True
        if self._max_per_key > 0 and self._key_running.get(key, 0) >= self._max_per_key:
            return True
        return False

    def try_reserve(self, key: str) -> bool:
        # issue #594: atomic admission-time reservation — True = slot held,
        # caller must eventually release(key). False = saturated or disabled.
        if not self.enabled or self.saturated(key):
            return False
        self._global_running += 1
        self._key_running[key] = self._key_running.get(key, 0) + 1
        return True

    async def acquire_reserved(self, key: str) -> None:
        # wait until a slot frees up, then reserve it (queued-run path);
        # disabled gate = unlimited slots, no-op
        if not self.enabled:
            return
        while not self.try_reserve(key):
            await asyncio.sleep(_POLL_INTERVAL)

    async def acquire(self, key: str) -> None:
        # back-compat entry: reserve now or wait for a slot
        await self.acquire_reserved(key)

    def release(self, key: str) -> None:
        self._global_running = max(0, self._global_running - 1)
        self._key_running[key] = max(0, self._key_running.get(key, 0) - 1)
        logger.debug(f"run gate released key={key} running={dict(self._key_running)}")

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_running": self._max_running,
            "max_per_key": self._max_per_key,
            "running_by_key": dict(self._key_running),
        }
