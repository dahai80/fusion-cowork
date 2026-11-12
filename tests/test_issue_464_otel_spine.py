"""issue #464: OTel spine — run 全程一个 trace_id, 出站 LLM 调用携带 trace 头。

引擎 trace_id=exec_id; 波次 span (无 SDK no-op 不阻断); traceparent W3C 格式;
轨迹事件携带 trace_id; MLX 客户端请求头合并 x-fusion-trace-id/traceparent。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx

from fusion_cowork.engine import PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.observability.trace import get_trace_id, new_trace_id, set_trace_id, trace_propagation_headers


@contextmanager
def _trace(tid):
    from fusion_cowork.observability.trace import _trace_var

    token = _trace_var.set(tid)
    try:
        yield
    finally:
        _trace_var.reset(token)


@register_node
class _Echo464(BaseNode):
    name = "echo464"
    display_name = "Echo 464"
    category = NodeCategory.LOGIC
    description = "test probe: captures trace id inside node execution (#464)"
    icon = "🧪"
    default_label = "Echo"

    seen = []

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).seen.append(get_trace_id())
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


class TestTracePropagationHeaders:
    def test_empty_without_trace(self):
        token = set_trace_id(None)
        try:
            assert trace_propagation_headers() == {}
        finally:
            from fusion_cowork.observability.trace import _trace_var

            _trace_var.reset(token)

    def test_w3c_format(self):
        tid = new_trace_id()
        with _trace(tid):
            h = trace_propagation_headers()
        assert h["x-fusion-trace-id"] == tid
        tp = h["traceparent"]
        version, trace_id, span_id, flags = tp.split("-")
        assert version == "00"
        assert len(trace_id) == 32
        assert len(span_id) == 16
        assert flags == "01"


class TestEngineTrace:
    async def test_run_trace_id_is_exec_id_and_reaches_nodes(self):
        _Echo464.seen = []
        eng = WorkflowEngine(permission_manager=PermissionManager())
        wf = Workflow(name="wf-464")
        wf.add_node(NodeRegistry.create("echo464"))
        exec_id = "run-464-trace"
        execution = await asyncio.wait_for(eng.execute(wf, execution_id=exec_id), timeout=10)
        assert execution.status.value == "success"
        assert _Echo464.seen == [exec_id], "节点内 trace_id 应等于 run exec_id"

    async def test_wave_spans_noop_without_otel(self):
        # span() 无 OTel SDK 时 no-op — 波次执行不被阻断
        eng = WorkflowEngine(permission_manager=PermissionManager())
        wf = Workflow(name="wf-464-span")
        wf.add_node(NodeRegistry.create("echo464"))
        execution = await asyncio.wait_for(eng.execute(wf, execution_id="run-464-span"), timeout=10)
        assert execution.status.value == "success"


class TestMlxClientHeaders:
    async def test_chat_sends_trace_headers(self, monkeypatch):
        import fusion_cowork.ai.mlx_client as mlxc
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        monkeypatch.setattr(mlxc, "_HAS_FUSION_CORE", False)

        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})

        transport = httpx.MockTransport(handler)
        client = FusionMLXClient(base_url="http://test")
        client._client = httpx.AsyncClient(base_url="http://test", transport=transport)
        tid = new_trace_id()
        with _trace(tid):
            await client.chat("m", [{"role": "user", "content": "x"}])
        h = captured["headers"]
        assert h.get("x-fusion-trace-id") == tid
        assert h.get("traceparent", "").startswith("00-")

    async def test_chat_no_trace_header_without_context(self, monkeypatch):
        import fusion_cowork.ai.mlx_client as mlxc
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        monkeypatch.setattr(mlxc, "_HAS_FUSION_CORE", False)
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})

        transport = httpx.MockTransport(handler)
        client = FusionMLXClient(base_url="http://test")
        client._client = httpx.AsyncClient(base_url="http://test", transport=transport)
        from fusion_cowork.observability.trace import _trace_var

        token = _trace_var.set(None)
        try:
            await client.chat("m", [{"role": "user", "content": "x"}])
        finally:
            _trace_var.reset(token)
        assert "x-fusion-trace-id" not in captured["headers"]


class TestTrajectoryTrace:
    async def test_recorder_writes_trace_id(self, tmp_path):

        from fusion_cowork.engine.hooks import HookEvent
        from fusion_cowork.trajectory.recorder import TrajectoryRecorder

        captured = []

        class _Writer:
            def write(self, evt):
                captured.append(evt)
                return "x"

        rec = TrajectoryRecorder(hook_manager=None, trajectory_dir=str(tmp_path))
        rec._writer = _Writer()
        rec._execs["e1"] = {"execution_id": "e1", "workflow_id": "w", "workflow_name": "n", "steps_snapshot": []}

        class _Ctx:
            data = {"execution_id": "e1", "node_id": "n1", "node_name": "echo", "status": "success"}

        tid = new_trace_id()
        with _trace(tid):
            await rec._on_hook(HookEvent.POST_NODE_EXECUTE, _Ctx())
        assert captured and captured[0].trace_id == tid
