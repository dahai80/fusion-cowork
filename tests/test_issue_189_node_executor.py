"""#189: 节点执行器抽象 + ready-set 波次并行。

覆盖:
- 默认 concurrency=1 与旧串行逐节点等价
- 3 个无依赖节点 concurrency=3 时墙钟 ≈ max 而非 sum
- DAG 屏障: 汇合节点等全部前驱完成
- FUSION_COWORK_MAX_CONCURRENCY 环境变量 / 非法值回退
- NodeConfig 执行器字段序列化往返
- AgentExecutor UDS 通道: 多轮循环、工具调用计数、逐节点覆盖参数透传
- AgentExecutor UDS 失败 / 缺任务文本 / socket 不可用
- AgentExecutor HTTP 回退: 结果读取、覆盖参数降级告警、超时取消
- 工作流内 agent 节点: step.executor、executors 分布、run 摘要
- agent 节点失败中止 run
- 断点续跑跨 agent 节点
- 未知执行器回退 simple
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from fusion_cowork.engine import executors as executors_mod
from fusion_cowork.engine.executors import (
    AgentExecutor,
    NodeExecutionContext,
    SimpleExecutor,
    executor_for_node,
    executor_names,
    get_executor,
)
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus

PARALLEL_STATE: dict = {}


@register_node
class _SleepNode(BaseNode):
    name = "sleep_189"
    display_name = "Sleep 189"
    category = NodeCategory.TOOL
    description = "测试用: 可控延迟节点"
    icon = "⏱️"
    default_label = "Sleep"

    def __init__(self, node_id: str = "", config: NodeConfig | None = None, delay: float = 0.1):
        super().__init__(node_id=node_id, config=config)
        self.delay = delay

    async def execute(self, inputs: dict) -> NodeResult:
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        end = time.monotonic()
        PARALLEL_STATE.setdefault("spans", {})[self.id] = (start, end)
        return NodeResult(status=NodeStatus.SUCCESS, data={"slept": self.delay}, summary="sleep")


@register_node
class _TrackNode(BaseNode):
    name = "track_189"
    display_name = "Track 189"
    category = NodeCategory.TOOL
    description = "测试用: 记录并发峰值"
    icon = "📈"
    default_label = "Track"

    def __init__(self, node_id: str = "", config: NodeConfig | None = None, delay: float = 0.05):
        super().__init__(node_id=node_id, config=config)
        self.delay = delay

    async def execute(self, inputs: dict) -> NodeResult:
        PARALLEL_STATE["active"] = PARALLEL_STATE.get("active", 0) + 1
        PARALLEL_STATE["peak"] = max(PARALLEL_STATE.get("peak", 0), PARALLEL_STATE["active"])
        try:
            await asyncio.sleep(self.delay)
        finally:
            PARALLEL_STATE["active"] -= 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"id": self.id}, summary="track")


@register_node
class _JoinNode(BaseNode):
    name = "join_189"
    display_name = "Join 189"
    category = NodeCategory.TOOL
    description = "测试用: 汇合节点"
    icon = "🔗"
    default_label = "Join"

    async def execute(self, inputs: dict) -> NodeResult:
        PARALLEL_STATE.setdefault("spans", {})[self.id] = (time.monotonic(), time.monotonic())
        return NodeResult(status=NodeStatus.SUCCESS, data={"keys": sorted(inputs.keys())}, summary="join")


@register_node
class _EchoNode(BaseNode):
    name = "echo_189"
    display_name = "Echo 189"
    category = NodeCategory.TOOL
    description = "测试用: 回显"
    icon = "🔁"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"input": inputs.get("input", "")}, summary="echo")


@register_node
class _BoomNode(BaseNode):
    name = "boom_189"
    display_name = "Boom 189"
    category = NodeCategory.TOOL
    description = "测试用: 直接失败"
    icon = "💥"
    default_label = "Boom"

    async def execute(self, inputs: dict) -> NodeResult:
        raise RuntimeError("boom-189")


@register_node
class _AgentProbeNode(BaseNode):
    name = "agent_probe_189"
    display_name = "Agent Probe 189"
    category = NodeCategory.AI_PROCESSING
    description = "测试用: 由 agent 执行器接管, 本体不应被调用"
    icon = "🤖"
    default_label = "Agent"

    async def execute(self, inputs: dict) -> NodeResult:
        PARALLEL_STATE["self_called"] = PARALLEL_STATE.get("self_called", 0) + 1
        return NodeResult(status=NodeStatus.FAILED, error="agent 节点本体被直接调用")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    PARALLEL_STATE.clear()
    for key in (
        "FUSION_COWORK_MAX_CONCURRENCY",
        "FUSION_AGENT_STUDIO_SOCKET",
        "FUSION_AGENT_STUDIO_URL",
        "FUSION_AGENT_STUDIO_API_KEY",
        "FUSION_COWORK_AGENT_TRANSPORT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    PARALLEL_STATE.clear()


def _sleep_wf(delays: dict) -> Workflow:
    wf = Workflow(name="parallel-189")
    for node_id, delay in delays.items():
        wf.add_node(_SleepNode(node_id=node_id, delay=delay))
    return wf


def _agent_config(**kwargs) -> NodeConfig:
    base = {"executor": "agent", "agent": "as-agent-1", "params": {"task": "do the thing"}}
    base.update(kwargs)
    return NodeConfig(**base)


class _FakeAsServer:
    def __init__(self, run_result: dict | None = None, transcript: dict | None = None):
        self.specs: list[dict] = []
        self.calls: list[str] = []
        self._run_result = (
            run_result
            if run_result is not None
            else {"report": {"ok": True, "report": "done", "turns": 3, "tokens_used": 120, "agent_id": "agent-x"}}
        )
        self._transcript = transcript or {
            "transcript": {
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": "go"}]},
                    {"role": "assistant", "content": [{"type": "tool_use", "name": "a"}]},
                    {"role": "assistant", "content": [{"type": "tool_use", "name": "b"}]},
                ]
            }
        }
        self._server = None
        self.path = ""

    async def _handle(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                frame = json.loads(line.decode())
                method = frame.get("method", "")
                self.calls.append(method)
                if method == "subagent.run":
                    self.specs.append(frame.get("params", {}).get("spec", {}))
                    result = self._run_result
                elif method == "subagent.transcript":
                    result = self._transcript
                else:
                    writer.write(
                        json.dumps(
                            {"jsonrpc": "2.0", "id": frame.get("id"), "error": {"message": f"unknown {method}"}}
                        ).encode()
                        + b"\n"
                    )
                    await writer.drain()
                    continue
                writer.write(json.dumps({"jsonrpc": "2.0", "id": frame.get("id"), "result": result}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self, monkeypatch) -> str:
        self.path = f"/tmp/cw189_{uuid.uuid4().hex[:8]}.sock"
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        monkeypatch.setenv("FUSION_AGENT_STUDIO_SOCKET", self.path)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "uds")
        return self.path

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)


@asynccontextmanager
async def _fake_as(monkeypatch, **kwargs):
    server = _FakeAsServer(**kwargs)
    await server.start(monkeypatch)
    try:
        yield server
    finally:
        await server.stop()


class TestSerialDefault:
    async def test_default_concurrency_serial_order(self):
        wf = Workflow(name="serial-189")
        wf.add_node(_EchoNode(node_id="a"))
        wf.add_node(_EchoNode(node_id="b"))
        wf.add_node(_EchoNode(node_id="c"))
        wf.connect("a", "b")
        wf.connect("b", "c")

        engine = WorkflowEngine()
        execution = await engine.execute(wf)

        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.concurrency == 1
        assert [s.node_id for s in execution.steps] == ["a", "b", "c"]
        assert execution.executors == {"simple": 3}
        assert all(s.executor == "simple" for s in execution.steps)
        assert "并发=1" in execution.result_summary
        assert "执行器=simple:3" in execution.result_summary

    async def test_default_serial_wall_is_sum(self):
        wf = _sleep_wf({"a": 0.1, "b": 0.1, "c": 0.1})
        engine = WorkflowEngine()
        start = time.monotonic()
        execution = await engine.execute(wf)
        wall = time.monotonic() - start
        assert execution.status == WorkflowStatus.SUCCESS
        assert wall >= 0.3
        assert [s.node_id for s in execution.steps] == ["a", "b", "c"]


class TestWaveParallel:
    async def test_three_independent_nodes_wall_equals_max(self):
        wf = _sleep_wf({"a": 0.3, "b": 0.3, "c": 0.3})
        engine = WorkflowEngine()
        start = time.monotonic()
        execution = await engine.execute(wf, concurrency=3)
        wall = time.monotonic() - start

        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.concurrency == 3
        assert wall < 0.6, f"并发未生效: {wall:.3f}s"
        spans = PARALLEL_STATE["spans"]
        assert set(spans.keys()) == {"a", "b", "c"}
        first_end = min(end for _, end in spans.values())
        last_start = max(start for start, _ in spans.values())
        assert last_start < first_end, "三个节点未重叠执行"
        # 确定性: 并发完成顺序不影响步骤落盘顺序
        assert [s.node_id for s in execution.steps] == ["a", "b", "c"]

    async def test_concurrency_cap_respected(self):
        wf = Workflow(name="cap-189")
        for idx in range(5):
            wf.add_node(_TrackNode(node_id=f"n{idx}"))
        engine = WorkflowEngine()
        execution = await engine.execute(wf, concurrency=2)
        assert execution.status == WorkflowStatus.SUCCESS
        assert PARALLEL_STATE["peak"] <= 2
        assert PARALLEL_STATE["peak"] >= 2, "并发上限未被用满"

    async def test_join_node_waits_for_all_preds(self):
        wf = Workflow(name="barrier-189")
        wf.add_node(_SleepNode(node_id="a", delay=0.15))
        wf.add_node(_SleepNode(node_id="b", delay=0.05))
        wf.add_node(_JoinNode(node_id="c"))
        wf.connect("a", "c")
        wf.connect("b", "c")

        engine = WorkflowEngine()
        execution = await engine.execute(wf, concurrency=3)
        assert execution.status == WorkflowStatus.SUCCESS
        spans = PARALLEL_STATE["spans"]
        c_start = spans["c"][0]
        assert c_start >= spans["a"][1]
        assert c_start >= spans["b"][1]
        # 落盘顺序仍按拓扑序 (a, b 同层按插入顺序, c 最后)
        assert execution.steps[-1].node_id == "c"

    async def test_env_concurrency_used_when_not_passed(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_MAX_CONCURRENCY", "3")
        wf = _sleep_wf({"a": 0.2, "b": 0.2, "c": 0.2})
        engine = WorkflowEngine()
        start = time.monotonic()
        execution = await engine.execute(wf)
        wall = time.monotonic() - start
        assert execution.concurrency == 3
        assert wall < 0.45

    async def test_invalid_env_falls_back_to_serial(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_MAX_CONCURRENCY", "abc")
        wf = _sleep_wf({"a": 0.05, "b": 0.05})
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.concurrency == 1

    async def test_explicit_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_MAX_CONCURRENCY", "5")
        wf = _sleep_wf({"a": 0.05, "b": 0.05})
        engine = WorkflowEngine()
        execution = await engine.execute(wf, concurrency=1)
        assert execution.concurrency == 1


class TestNodeConfigExecutorFields:
    def test_roundtrip(self):
        node = _AgentProbeNode(
            node_id="agent-1",
            config=NodeConfig(
                executor="agent",
                agent="reviewer",
                tools=["read_file", "grep"],
                model="claude-sonnet-5-5",
                max_turns=6,
                budget_tokens=4096,
            ),
        )
        data = node.to_dict()
        cfg = data["config"]
        assert cfg["executor"] == "agent"
        assert cfg["agent"] == "reviewer"
        assert cfg["tools"] == ["read_file", "grep"]
        assert cfg["model"] == "claude-sonnet-5-5"
        assert cfg["max_turns"] == 6
        assert cfg["budget_tokens"] == 4096

        restored = _AgentProbeNode.from_dict(data)
        assert restored.config.executor == "agent"
        assert restored.config.agent == "reviewer"
        assert restored.config.tools == ["read_file", "grep"]
        assert restored.config.model == "claude-sonnet-5-5"
        assert restored.config.max_turns == 6
        assert restored.config.budget_tokens == 4096

    def test_defaults_are_simple(self):
        node = _EchoNode(node_id="plain")
        assert node.config.executor == "simple"
        assert node.config.agent == ""
        assert node.config.tools == []
        assert node.config.max_turns == 0
        assert node.config.budget_tokens == 0

    def test_missing_fields_default_on_old_payload(self):
        payload = {"id": "old", "name": "echo_189", "config": {"label": "旧节点"}}
        node = _EchoNode.from_dict(payload)
        assert node.config.executor == "simple"
        assert node.config.tools == []
        assert node.config.max_turns == 0

    def test_get_executor_unknown_falls_back_simple(self):
        assert isinstance(get_executor("bogus"), SimpleExecutor)
        assert isinstance(get_executor(""), SimpleExecutor)
        assert isinstance(get_executor("AGENT"), AgentExecutor)

    def test_executor_names_counts(self):
        nodes = [_EchoNode(), _AgentProbeNode(config=_agent_config()), _SleepNode(delay=0)]
        assert executor_names(nodes) == {"simple": 2, "agent": 1}

    def test_executor_for_node_unknown(self):
        node = _EchoNode(config=NodeConfig(executor="nope"))
        assert executor_for_node(node).name == "simple"


class TestSimpleExecutor:
    async def test_delegates_to_node(self):
        node = _EchoNode(node_id="e1")
        result = await SimpleExecutor().execute(node, {"input": "hi"}, NodeExecutionContext(node_id="e1"))
        assert result.status == NodeStatus.SUCCESS
        assert result.data == {"input": "hi"}


class TestAgentExecutorUds:
    async def test_success_multi_turn_with_tool_calls(self, monkeypatch):
        async with _fake_as(monkeypatch) as server:
            node = _AgentProbeNode(
                node_id="ag1",
                config=_agent_config(
                    tools=["read_file", "grep"],
                    model="claude-sonnet-5-5",
                    max_turns=5,
                    budget_tokens=2048,
                ),
            )
            result = await AgentExecutor().execute(
                node, {"input": "summarize repo"}, NodeExecutionContext(node_id="ag1")
            )

        assert result.status == NodeStatus.SUCCESS, result.error
        assert result.data["executor"] == "agent"
        assert result.data["transport"] == "uds"
        assert result.data["turns"] == 3
        assert result.data["tool_calls"] == 2
        assert result.data["usage"]["total_tokens"] == 120
        assert "turns=3" in result.summary
        assert "tool_calls=2" in result.summary
        assert server.calls == ["subagent.run", "subagent.transcript"]
        spec = server.specs[0]
        assert spec["name"] == "as-agent-1"
        assert spec["task"] == "summarize repo"
        assert spec["tools"] == ["read_file", "grep"]
        assert spec["model"] == "claude-sonnet-5-5"
        assert spec["max_turns"] == 5
        assert spec["budget_tokens"] == 2048
        assert PARALLEL_STATE.get("self_called", 0) == 0

    async def test_partial_run_still_success_with_note(self, monkeypatch):
        run_result = {"report": {"ok": True, "report": "partial", "turns": 1, "tokens_used": 10, "partial": True}}
        async with _fake_as(monkeypatch, run_result=run_result):
            node = _AgentProbeNode(node_id="ag1", config=_agent_config())
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.SUCCESS
        assert "partial" in result.summary

    async def test_report_ok_false_fails(self, monkeypatch):
        run_result = {"report": {"ok": False, "error": "max turns exceeded", "turns": 8, "tokens_used": 900}}
        async with _fake_as(monkeypatch, run_result=run_result):
            node = _AgentProbeNode(node_id="ag1", config=_agent_config())
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "max turns exceeded" in result.error
        assert result.data["turns"] == 8

    async def test_missing_report_fails(self, monkeypatch):
        async with _fake_as(monkeypatch, run_result={}):
            node = _AgentProbeNode(node_id="ag1", config=_agent_config())
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "未返回 report" in result.error

    async def test_transcript_failure_does_not_fail_run(self, monkeypatch):
        async with _fake_as(monkeypatch) as server:
            original = server._handle

            async def handler(reader, writer):
                line = await reader.readline()
                frame = json.loads(line.decode())
                server.calls.append(frame.get("method", ""))
                if frame.get("method") == "subagent.transcript":
                    writer.write(
                        json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "no transcript"}}).encode() + b"\n"
                    )
                else:
                    server.specs.append(frame.get("params", {}).get("spec", {}))
                    writer.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "result": {"report": {"ok": True, "turns": 2, "tokens_used": 5, "agent_id": "a"}},
                            }
                        ).encode()
                        + b"\n"
                    )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server._server.close()
            await server._server.wait_closed()
            server._server = await asyncio.start_unix_server(handler, path=server.path)
            node = _AgentProbeNode(node_id="ag1", config=_agent_config())
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.SUCCESS
        assert result.data["tool_calls"] == 0
        assert original is not None

    async def test_missing_task_text_fails(self, monkeypatch):
        async with _fake_as(monkeypatch) as server:
            node = _AgentProbeNode(node_id="ag1", config=NodeConfig(executor="agent"))
            result = await AgentExecutor().execute(node, {}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "缺少任务文本" in result.error
        assert server.calls == []

    async def test_missing_socket_fails(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "uds")
        monkeypatch.setenv("FUSION_AGENT_STUDIO_SOCKET", "/tmp/cw189_missing_socket.sock")
        node = _AgentProbeNode(node_id="ag1", config=_agent_config())
        result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "socket 不可用" in result.error

    async def test_rpc_error_fails_visibly(self, monkeypatch):
        async def handler(reader, writer):
            await reader.readline()
            writer.write(
                json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "daemon overloaded"}}).encode() + b"\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        path = f"/tmp/cw189_{uuid.uuid4().hex[:8]}.sock"
        server = await asyncio.start_unix_server(handler, path=path)
        monkeypatch.setenv("FUSION_AGENT_STUDIO_SOCKET", path)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "uds")
        try:
            node = _AgentProbeNode(node_id="ag1", config=_agent_config())
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        finally:
            server.close()
            await server.wait_closed()
            if os.path.exists(path):
                os.unlink(path)
        assert result.status == NodeStatus.FAILED
        assert "daemon overloaded" in result.error


class TestAgentExecutorHttp:
    def _patch_client(self, monkeypatch, handler):
        real_client = httpx.AsyncClient
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(executors_mod.httpx, "AsyncClient", factory)
        return captured

    async def test_http_completed_run(self, monkeypatch):
        seen: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, {**request.url.params}))
            if request.method == "POST" and request.url.path == "/v1/runs":
                return httpx.Response(200, json={"run_id": "run-1"})
            if request.url.path == "/v1/runs/run-1":
                return httpx.Response(200, json={"run_id": "run-1", "status": "completed"})
            if request.url.path == "/v1/runs/run-1/result":
                return httpx.Response(200, json={"last_result": {"answer": 42}, "steps": [{"events": 7}]})
            return httpx.Response(404, json={"error": "nope"})

        captured = self._patch_client(monkeypatch, handler)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "http")
        monkeypatch.setenv("FUSION_AGENT_STUDIO_URL", "http://127.0.0.1:19999")
        node = _AgentProbeNode(node_id="ag1", config=_agent_config())
        result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))

        assert result.status == NodeStatus.SUCCESS, result.error
        assert result.data["transport"] == "http"
        assert result.data["run_id"] == "run-1"
        assert result.data["status"] == "completed"
        assert result.data["events"] == 7
        assert result.data["result"] == {"answer": 42}
        assert captured["base_url"] == "http://127.0.0.1:19999"
        assert ("POST", {}) in seen

    async def test_http_ignores_per_node_overrides_with_warning(self, monkeypatch, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/runs":
                return httpx.Response(200, json={"run_id": "run-2"})
            if request.url.path == "/v1/runs/run-2":
                return httpx.Response(200, json={"status": "completed"})
            return httpx.Response(200, json={"last_result": {}})

        self._patch_client(monkeypatch, handler)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "http")
        node = _AgentProbeNode(
            node_id="ag1", config=_agent_config(tools=["grep"], model="m", max_turns=4, budget_tokens=100)
        )
        with caplog.at_level("WARNING", logger=executors_mod.__name__):
            result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.SUCCESS
        assert any("不支持逐节点覆盖" in rec.message for rec in caplog.records)

    async def test_http_terminal_failure(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/runs":
                return httpx.Response(200, json={"run_id": "run-3"})
            if request.url.path == "/v1/runs/run-3":
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(200, json={"last_error": "graph exploded"})

        self._patch_client(monkeypatch, handler)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "http")
        node = _AgentProbeNode(node_id="ag1", config=_agent_config())
        result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "graph exploded" in result.error

    async def test_http_timeout_cancels(self, monkeypatch):
        cancelled: list[str] = []
        polls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/runs":
                return httpx.Response(200, json={"run_id": "run-4"})
            if request.url.path == "/v1/runs/run-4/cancel":
                cancelled.append("run-4")
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/runs/run-4":
                polls["n"] += 1
                return httpx.Response(200, json={"status": "running"})
            return httpx.Response(200, json={})

        self._patch_client(monkeypatch, handler)
        monkeypatch.setattr(executors_mod, "_AS_POLL_INTERVAL", 0.01)
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "http")
        node = _AgentProbeNode(node_id="ag1", config=_agent_config(timeout=0.05))
        result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "超时" in result.error
        assert cancelled == ["run-4"]

    async def test_http_requires_agent_or_graph_id(self, monkeypatch):
        self._patch_client(monkeypatch, lambda request: httpx.Response(500))
        monkeypatch.setenv("FUSION_COWORK_AGENT_TRANSPORT", "http")
        node = _AgentProbeNode(node_id="ag1", config=NodeConfig(executor="agent"))
        result = await AgentExecutor().execute(node, {"task": "go"}, NodeExecutionContext(node_id="ag1"))
        assert result.status == NodeStatus.FAILED
        assert "agent" in result.error and "graph_id" in result.error


class TestAgentNodeInWorkflow:
    async def test_mixed_dag_executor_mix_and_summary(self, monkeypatch):
        async with _fake_as(monkeypatch) as server:
            wf = Workflow(name="mixed-189")
            wf.add_node(_EchoNode(node_id="src"))
            wf.add_node(_AgentProbeNode(node_id="ag1", config=_agent_config()))
            wf.add_node(_EchoNode(node_id="sink"))
            wf.connect("src", "ag1")
            wf.connect("ag1", "sink")

            engine = WorkflowEngine()
            execution = await engine.execute(wf)

        assert execution.status == WorkflowStatus.SUCCESS, execution.error
        assert [s.node_id for s in execution.steps] == ["src", "ag1", "sink"]
        assert [s.executor for s in execution.steps] == ["simple", "agent", "simple"]
        assert execution.executors == {"simple": 2, "agent": 1}
        assert "并发=1" in execution.result_summary
        assert "agent:1" in execution.result_summary
        assert "simple:2" in execution.result_summary
        assert PARALLEL_STATE.get("self_called", 0) == 0
        assert server.specs[0]["task"], "agent 节点应收到上游输出作为任务文本"

    async def test_agent_failure_stops_run(self, monkeypatch):
        run_result = {"report": {"ok": False, "error": "agent gave up", "turns": 1}}
        async with _fake_as(monkeypatch, run_result=run_result):
            wf = Workflow(name="agent-fail-189")
            wf.add_node(_AgentProbeNode(node_id="ag1", config=_agent_config()))
            wf.add_node(_EchoNode(node_id="after"))
            wf.connect("ag1", "after")

            engine = WorkflowEngine()
            execution = await engine.execute(wf)

        assert execution.status == WorkflowStatus.FAILED
        assert "agent gave up" in (execution.error or "")
        assert [s.node_id for s in execution.steps] == ["ag1"]

    async def test_resume_skips_agent_node(self, monkeypatch):
        async with _fake_as(monkeypatch) as server:
            wf = Workflow(name="resume-agent-189")
            wf.add_node(_AgentProbeNode(node_id="ag1", config=_agent_config()))
            wf.add_node(_EchoNode(node_id="after"))
            wf.connect("ag1", "after")

            resume = [
                {
                    "node_id": "ag1",
                    "node_name": "agent_probe_189",
                    "status": "success",
                    "output_data": {"result": "cached agent output"},
                }
            ]
            engine = WorkflowEngine()
            execution = await engine.execute(wf, resume_steps=resume)

        assert execution.status == WorkflowStatus.SUCCESS
        assert [s.node_id for s in execution.steps] == ["ag1", "after"]
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert execution.steps[0].executor == "agent"
        assert execution.steps[1].status == NodeStatus.SUCCESS
        assert execution.steps[1].input_data.get("result") == "cached agent output"
        assert server.calls == [], "续跑不应重新调用 agent-studio"

    async def test_unknown_executor_runs_as_simple(self):
        wf = Workflow(name="unknown-exec-189")
        wf.add_node(_EchoNode(node_id="e1", config=NodeConfig(executor="definitely-not-real")))
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].executor == "simple"

    async def test_parallel_failure_still_reports_all_steps(self):
        wf = Workflow(name="parallel-fail-189")
        wf.add_node(_SleepNode(node_id="a", delay=0.05))
        wf.add_node(_BoomNode(node_id="b"))
        wf.add_node(_SleepNode(node_id="c", delay=0.05))
        engine = WorkflowEngine()
        execution = await engine.execute(wf, concurrency=3)
        assert execution.status == WorkflowStatus.FAILED
        assert "boom-189" in (execution.error or "")
        # 同波全部节点结果都要落盘, 不能因一个失败丢掉兄弟节点
        assert {s.node_id for s in execution.steps} == {"a", "b", "c"}
        failed = next(s for s in execution.steps if s.node_id == "b")
        assert failed.status == NodeStatus.FAILED
        assert "boom-189" in (failed.error or "")


LIVE_AS = os.environ.get("FUSION_COWORK_LIVE_AGENT_STUDIO", "").strip() == "1"


@pytest.mark.skipif(not LIVE_AS, reason="真实 agent-studio daemon 集成测试, 需 FUSION_COWORK_LIVE_AGENT_STUDIO=1")
class TestLiveAgentStudio:
    async def test_real_agent_run(self):
        node = _AgentProbeNode(
            node_id="live-ag",
            config=NodeConfig(
                executor="agent",
                agent=os.environ.get("FUSION_COWORK_LIVE_AGENT", "cowork-live"),
                max_turns=2,
                params={"task": "reply with the word pong"},
            ),
        )
        result = await AgentExecutor().execute(node, {"task": "reply with the word pong"}, NodeExecutionContext())
        assert result.status == NodeStatus.SUCCESS, result.error
        assert result.data["transport"] in ("uds", "http")
