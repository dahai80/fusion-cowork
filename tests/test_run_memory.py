# RunRegistry × memory_bridge 集成测试 (issue #157)。
# 验收对齐 fusion-memory#20: 开 → 写回+注入闭环; 关 → 零 RPC 零痕迹; 桥挂 → run 不炸。

import json

import httpx
import pytest

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry
from tests.test_engine import MockSuccessNode

MARKER = "上周产出: 财报摘要 v2"


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _ok_transport_handler(retrieve_text: str, commit_ids=None):
    # retrieve 返 MARKER 块; commit 返新契约 memory_ids。记录收到的方法序列。
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
    wf = Workflow(name="财报任务", description="整理季度财报")
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

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    # 注入: 首节点输入含 memory_context (MockSuccessNode 回显 inputs)
    assert record["memory_context"] == MARKER
    assert record["steps"][0]["output_data"]["input"]["memory_context"] == MARKER
    # 写回: retrieve + commit 各一次, run record 可见
    assert calls["methods"] == ["retrieve", "commit"]
    assert record["memory_writeback"] == "ok"
    assert record["memory_ids"] == ["mem-1"]


@pytest.mark.asyncio
async def test_run_memory_off_zero_trace(monkeypatch):
    # 开关关: 即使 key 已配也不发任何 RPC, run record 无记忆痕迹
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=False)
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert record["memory"] is False
    assert calls["methods"] == []
    assert "memory_context" not in record
    assert "memory_writeback" not in record
    assert "memory_ids" not in record


@pytest.mark.asyncio
async def test_run_memory_unconfigured_key_silent_skip(monkeypatch):
    # 未配 key: 桥整体关闭, 零 RPC, 行为与旧版一致
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert calls["methods"] == []
    assert "memory_context" not in record


@pytest.mark.asyncio
async def test_run_memory_bridge_down_run_survives(monkeypatch):
    # fusion-memory 挂: retrieve/commit 全 500 → run 照常成功, 写回状态 fail-visible
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    _patch_transport(monkeypatch, handler)
    registry, wf_def = _make_registry_and_def()

    ret = registry.start(wf_def, workflow_id="wf1", workflow_name="财报任务", memory=True)
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "success"
    assert "memory_context" not in record
    assert record["memory_writeback"] == "failed"
    assert "memory_ids" not in record


@pytest.mark.asyncio
async def test_run_memory_failed_run_no_writeback(monkeypatch):
    # 失败 run 不写回 (只沉淀有效产出), 但启动召回仍执行
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

    @register_node
    class MockBoomNode(BaseNode):
        name = "mock_boom_memory_test"
        display_name = "Mock 爆炸"
        category = NodeCategory.IO
        description = "测试用：总是失败"
        icon = "🧪"
        default_label = "Mock 爆炸"

        async def execute(self, inputs: dict) -> NodeResult:
            return NodeResult(status=NodeStatus.FAILED, error="boom", summary="boom")

    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    handler, calls = _ok_transport_handler(MARKER)
    _patch_transport(monkeypatch, handler)
    engine = WorkflowEngine()
    registry = RunRegistry(engine)
    wf = Workflow(name="会炸任务", description="")
    wf.add_node(MockBoomNode(node_id="n1"))

    ret = registry.start(wf.to_dict(), workflow_id="wf2", workflow_name="会炸任务", memory=True)
    record = await _wait_run(registry, ret["run_id"])

    assert record["status"] == "failed"
    assert calls["methods"] == ["retrieve"]
    assert "memory_writeback" not in record
