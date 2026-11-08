"""Chaos S4: long generation + caller cancel (issue #386, spec §3.2 row 4).

Source defect: gw#577 — caller cancel during a long generation must not
open the repo's route breaker.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

import httpx
import pytest

from fusion_cowork.engine.node import NodeConfig
from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode
from fusion_cowork.security.circuit_breaker import CircuitBreaker

pytestmark = [pytest.mark.chaos]


def _studio_mock(hits: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        hits["requests"].append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "run-x", "status": "pending"})
        if request.method == "GET" and request.url.path == "/v1/runs/run-x":
            return httpx.Response(200, json={"run_id": "run-x", "status": "running"})
        return httpx.Response(404, json={"error": "unexpected"})

    return handler


def _patch_client(node_module, transport):
    orig = httpx.AsyncClient.__init__

    def _patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        orig(self, *args, **kwargs)

    return mock.patch.object(node_module.httpx.AsyncClient, "__init__", _patched)


def _make_node() -> StudioRunNode:
    return StudioRunNode(
        node_id="n1",
        config=NodeConfig(params={"mode": "sync", "poll_interval": 0.01, "timeout": 700.0}),
    )


@pytest.mark.asyncio
async def test_cancel_does_not_cancel_upstream_or_open_breaker():
    hits = {"requests": []}
    transport = httpx.MockTransport(_studio_mock(hits))
    node = _make_node()

    import fusion_cowork.nodes.ecosystem.studio_node as studio_mod

    with _patch_client(studio_mod, transport):
        breaker = CircuitBreaker(name="route", failure_threshold=2, recovery_timeout=1.0)
        task = asyncio.create_task(breaker.call(node.execute, {"input": "long generation", "agent_id": "a1"}))

        await asyncio.sleep(0.05)
        assert not task.done(), "poll loop should still be waiting on the long generation"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not any(m == "DELETE" or "cancel" in p for m, p in hits["requests"]), (
            "caller cancel must not cancel the upstream run"
        )
        assert breaker.is_open() is False

    assert ("GET", "/v1/runs/run-x") in hits["requests"]
