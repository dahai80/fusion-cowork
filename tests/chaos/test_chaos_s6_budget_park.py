"""Chaos S6: budget exhaustion → park → resume (issue #646, spec verification-infra-1108 §3.2 row 6).

Verifies that a "parked" upstream run is treated as non-terminal: the node
keeps polling until the run resumes and completes, instead of failing the
node or reporting success early.
"""

from __future__ import annotations

import time
import unittest.mock as mock

import httpx
import pytest

from fusion_cowork.engine.node import NodeConfig, NodeStatus
from fusion_cowork.nodes.ecosystem.studio_node import _TERMINAL_STATUSES, StudioRunNode

pytestmark = [pytest.mark.chaos]


def _studio_mock(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        state["requests"].append((method, path))
        if method == "POST" and path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "run-p", "status": "pending"})
        if method == "GET" and path == "/v1/runs/run-p":
            state["polls"] += 1
            if state["polls"] <= 2:
                state["statuses"].append("parked")
                return httpx.Response(
                    200,
                    json={
                        "run_id": "run-p",
                        "status": "parked",
                        "resumes_at": time.time() + 3600,
                    },
                )
            state["statuses"].append("completed")
            return httpx.Response(200, json={"run_id": "run-p", "status": "completed"})
        if method == "GET" and path == "/v1/runs/run-p/result":
            state["statuses"].append("completed")
            return httpx.Response(200, json={"run_id": "run-p", "artifacts": [], "last_error": ""})
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
        node_id="n-park",
        config=NodeConfig(params={"mode": "sync", "poll_interval": 0.01, "timeout": 5.0}),
    )


def test_parked_is_not_terminal():
    assert "parked" not in _TERMINAL_STATUSES, "parked run keeps polling, must not be terminal"
    assert frozenset({"completed", "failed", "canceled"}) == _TERMINAL_STATUSES


@pytest.mark.asyncio
async def test_parked_run_polls_until_completed():
    state = {"polls": 0, "statuses": [], "requests": []}
    transport = httpx.MockTransport(_studio_mock(state))
    node = _make_node()

    import fusion_cowork.nodes.ecosystem.studio_node as studio_mod

    with _patch_client(studio_mod, transport):
        result = await node.execute({"input": "budget park scenario", "agent_id": "a1"})

    assert result.status is NodeStatus.SUCCESS, f"node must succeed after resume, got: {result.error}"
    assert result.data["status"] == "completed"
    assert state["polls"] >= 3, f"handler must see at least 3 status polls, saw {state['polls']}"
    assert "parked" in state["statuses"], "node must have observed the parked status at least once"
    assert "completed" in state["statuses"], "node must have observed completion after park"
    assert "failed" not in state["statuses"], "node must not fail while parked"
