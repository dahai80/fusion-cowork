# RunRegistry x memory_bridge integration tests (issue #157).
# Acceptance aligned with fusion-memory#20: on -> write-back + injection loop; off -> zero RPC, zero trace; bridge down -> run survives.

import json

import httpx
import pytest

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry
from tests.test_engine import MockSuccessNode

MARKER = "上周产出: 财报摘要 v2"  # i18n: user-facing


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _ok_transport_handler(retrieve_text: str, commit_ids=None):
    # retrieve returns the MARKER block; commit returns the new-contract memory_ids. Records the received method sequence.
    calls = {"methods": []}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls["methods"].append(payload["method"])
        if payload["method"] == "retrieve":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {"blocks": [{"turns_text": retrieve_text}], "total_tokens": 10},
                    "id": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"memory_ids": commit_ids or ["mem-1"], "failed_turns": []},
                "id": 1,
            },
        )

    return handler, calls


def _make_registry_and_def():
    engine = WorkflowEngine()
    registry = RunRegistry(engine)
    wf = Workflow(name="财报任务", description="整理季度财报")  # i18n: user-facing
    wf.add_node(MockSuccessNode(node_id="n1"))
    return registry, wf.to_dict()


async def _wait_run(registry: RunRegistry, run_id: str) -> dict:
    await registry._tasks[run_id]
    return registry.get(run_id)


@pytest.mark.asyncio
async def test_run_memory_on_inject_and_write_back(monkeypatch):
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)  # i18n: user-facing
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    # injection: first node input contains memory_context (MockSuccessNode echoes inputs)
    assert record["memory_context"] == MARKER
    assert record["steps"][0]["output_data"]["input"]["memory_context"] == MARKER
    # write-back: retrieve + commit once each, visible in the run record
    assert calls["methods"] == ["retrieve", "commit"]
    assert record["memory_writeback"] == "ok"
    assert record["memory_ids"] == ["mem-1"]


@pytest.mark.asyncio
async def test_run_memory_off_zero_trace(monkeypatch):
    # switch off: no RPC even with a key configured; run record carries no memory trace
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=False)  # i18n: user-facing
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert record["memory"] is False
    assert calls["methods"] == []
    assert "memory_context" not in record
    assert "memory_writeback" not in record
    assert "memory_ids" not in record


@pytest.mark.asyncio
async def test_run_memory_unconfigured_key_silent_skip(monkeypatch):
    # no key configured: bridge entirely off, zero RPC, behavior matches the legacy version
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)  # i18n: user-facing
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert calls["methods"] == []
    assert "memory_context" not in record


@pytest.mark.asyncio
async def test_run_memory_bridge_down_run_survives(monkeypatch):
    # fusion-memory down: retrieve/commit all 500 -> run still succeeds, write-back status fail-visible
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)  # i18n: user-facing
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert "memory_context" not in record
    assert record["memory_writeback"] == "failed"
    assert "memory_ids" not in record


@pytest.mark.asyncio
async def test_run_memory_failed_run_no_writeback(monkeypatch):
    # failed run is not written back (only valid output settles), but startup recall still runs
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

    @register_node
    class MockBoomNode(BaseNode):
        name = "mock_boom_memory_test"
        display_name = "Mock 爆炸"  # i18n: user-facing
        category = NodeCategory.IO
        description = "测试用：总是失败"  # i18n: user-facing
        icon = "🧪"
        default_label = "Mock 爆炸"  # i18n: user-facing

        async def execute(self, inputs: dict) -> NodeResult:
            return NodeResult(status=NodeStatus.FAILED, error="boom", summary="boom")

    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    engine = WorkflowEngine()
    registry = RunRegistry(engine)
    wf = Workflow(name="会炸任务", description="")  # i18n: user-facing
    wf.add_node(MockBoomNode(node_id="n1"))

    ret = registry.start(wf.to_dict(), workflow_id="wf2", workflow_name="会炸任务", memory=True)  # i18n: user-facing
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "failed"
    assert calls["methods"] == ["retrieve"]
    assert "memory_writeback" not in record
