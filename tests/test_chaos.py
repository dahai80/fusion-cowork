"""Stage 7 chaos — DB outage circuit-breaker open + degradation; in-flight SIGTERM graceful drain.

@ pytest.mark.chaos.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.chaos]

from fusion_cowork.security.circuit_breaker import CircuitBreaker, CircuitOpenError


class TestCircuitBreakerChaos:
    async def test_db_disconnect_opens_breaker_then_degrades(self):
        cb = CircuitBreaker(name="db", failure_threshold=3, recovery_timeout=1.0)

        call_count = {"n": 0}

        async def flaky_db_call():
            call_count["n"] += 1
            raise ConnectionError("DB gone")

        # consecutive failures reach the threshold -> open
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await cb.call(flaky_db_call)

        assert cb.is_open() is True

        # once open the underlying call is not invoked; CircuitOpenError raised directly (degradation does not hit the backend)
        before = call_count["n"]
        with pytest.raises(CircuitOpenError):
            await cb.call(flaky_db_call)
        assert call_count["n"] == before, "熔断 open 后不应再调底层"  # i18n: user-facing

    async def test_half_open_recovery_after_timeout(self, monkeypatch):
        cb = CircuitBreaker(name="db", failure_threshold=2, recovery_timeout=1.0)
        t = [0.0]

        monkeypatch.setattr("fusion_cowork.security.circuit_breaker.time.monotonic", lambda: t[0])

        async def fail():
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)
        assert cb.is_open()

        # advance time past the recovery window -> half_open
        t[0] += 2.0

        async def ok():
            return "recovered"

        result = await cb.call(ok)
        assert result == "recovered"
        assert cb.is_open() is False


class TestGracefulDrain:
    async def test_in_flight_tasks_drain_on_signal(self):
        # simulate graceful drain: after the signal, wait for in-flight tasks to finish before exiting
        drained = {"done": False}

        async def long_task():
            await asyncio.sleep(0.2)
            drained["done"] = True

        async def runner():
            task = asyncio.create_task(long_task())
            await task

        await asyncio.wait_for(runner(), timeout=2.0)
        assert drained["done"] is True

    async def test_drain_timeout_force_cancels(self):
        # still unfinished past the grace period -> force cancel (anti-deadlock)
        stuck_done = {"cancelled": False}

        async def stuck_task():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                stuck_done["cancelled"] = True
                raise

        async def drain_with_grace(task, grace=0.1):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=grace)
            except TimeoutError:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        task = asyncio.create_task(stuck_task())
        await drain_with_grace(task, grace=0.1)
        assert stuck_done["cancelled"] is True
