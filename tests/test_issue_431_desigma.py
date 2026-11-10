"""issue #431: desigma-mcp 接入 — mcp_client stdio 传输 + design_context 节点 + product_lifecycle 挂载。

离线桩: 伪 asyncio 子进程回放 newline-delimited JSON-RPC; 无真实 desigma-mcp。
红线: 零设计稿 (workspace 空) 空转成功, 管线行为不变; spawn 由
FUSION_COWORK_MCP_STDIO_ALLOW 白名单把关, 缺省空 = 一律拒绝 (fail-closed)。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fusion_cowork.engine.node import NodeConfig, NodeStatus
from fusion_cowork.nodes.ecosystem import mcp_stdio
from fusion_cowork.nodes.ecosystem.desigma_node import DesignContextNode
from fusion_cowork.nodes.ecosystem.mcp_client_node import MCPClientNode


class _FakeWriter:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(data.decode("utf-8"))

    async def drain(self):
        return None


class _FakeReader:
    def __init__(self, replies):
        self._replies = list(replies)

    async def readline(self):
        if not self._replies:
            return b""
        return self._replies.pop(0)


class _FakeProc:
    def __init__(self, replies):
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader(replies)
        self.terminated = False
        self.kill_called = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.kill_called = True

    async def wait(self):
        return 0


def _reply(req_id, result):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode("utf-8") + b"\n"


def _tool_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


@pytest.fixture
def allow_env(monkeypatch):
    monkeypatch.setenv(mcp_stdio.STDIO_ALLOW_ENV, "desigma-mcp,other-tool")
    return monkeypatch


class TestStdioAllowlist:
    def test_empty_allowlist_denies_all(self, monkeypatch):
        monkeypatch.delenv(mcp_stdio.STDIO_ALLOW_ENV, raising=False)
        assert mcp_stdio.stdio_allowed("desigma-mcp") is False

    def test_basename_exact_match(self, allow_env):
        assert mcp_stdio.stdio_allowed("desigma-mcp") is True
        assert mcp_stdio.stdio_allowed("/usr/local/bin/desigma-mcp") is True
        assert mcp_stdio.stdio_allowed("evil-tool") is False

    def test_allowlist_parsing(self, monkeypatch):
        monkeypatch.setenv(mcp_stdio.STDIO_ALLOW_ENV, "a ; b, c")
        assert mcp_stdio.stdio_allowlist() == ["a", "b", "c"]


class TestStdioToolCalls:
    def test_happy_path(self, monkeypatch):
        proc = _FakeProc(
            [
                _reply(1, {"protocolVersion": "2025-03-26", "serverInfo": {"name": "desigma-mcp"}}),
                _reply(2, _tool_result("doc text")),
                _reply(3, _tool_result("code body")),
            ]
        )

        async def fake_exec(*a, **kw):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        results = asyncio.run(
            mcp_stdio.stdio_tool_calls(
                "desigma-mcp", ["--workspace", "/w"], [{"tool": "t1"}, {"tool": "t2", "arguments": {"x": 1}}]
            )
        )
        assert [r["tool"] for r in results] == ["t1", "t2"]
        assert results[0]["text"] == "doc text"
        assert results[1]["text"] == "code body"
        assert proc.terminated is True
        init_line = json.loads(proc.stdin.lines[0])
        assert init_line["method"] == "initialize"
        note_line = json.loads(proc.stdin.lines[1])
        assert note_line["method"] == "notifications/initialized"

    def test_tool_error_raises_and_terminates(self, monkeypatch):
        proc = _FakeProc(
            [
                _reply(1, {"serverInfo": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "no such tool"}}).encode()
                + b"\n",
            ]
        )

        async def fake_exec(*a, **kw):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(RuntimeError, match="no such tool"):
            asyncio.run(mcp_stdio.stdio_tool_calls("desigma-mcp", [], [{"tool": "missing"}]))
        assert proc.terminated is True

    def test_iserror_raises(self, monkeypatch):
        proc = _FakeProc([_reply(1, {"serverInfo": {}}), _reply(2, {"content": [], "isError": True})])

        async def fake_exec(*a, **kw):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(RuntimeError, match="isError"):
            asyncio.run(mcp_stdio.stdio_tool_calls("desigma-mcp", [], [{"tool": "boom"}]))


class TestMcpClientStdio:
    def test_stdio_mode_success(self, allow_env):
        proc = _FakeProc([_reply(1, {"serverInfo": {}}), _reply(2, _tool_result("frame json"))])

        async def fake_exec(*a, **kw):
            return proc

        allow_env.setattr(asyncio, "create_subprocess_exec", fake_exec)
        node = MCPClientNode(
            config=NodeConfig(
                params={
                    "transport": "stdio",
                    "command": "desigma-mcp",
                    "args": ["--workspace", "/w"],
                    "tool": "desigma_get_document",
                    "arguments": {"session": "file"},
                }
            )
        )
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS
        assert result.data["result"] == "frame json"

    def test_stdio_mode_denied_without_allowlist(self, monkeypatch):
        monkeypatch.delenv(mcp_stdio.STDIO_ALLOW_ENV, raising=False)
        node = MCPClientNode(
            config=NodeConfig(params={"transport": "stdio", "command": "arbitrary-bin", "tool": "x", "arguments": {}})
        )
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED
        assert "FUSION_COWORK_MCP_STDIO_ALLOW" in result.error

    def test_stdio_mode_json_string_arguments(self, allow_env):
        proc = _FakeProc([_reply(1, {"serverInfo": {}}), _reply(2, _tool_result("ok"))])

        async def fake_exec(*a, **kw):
            return proc

        allow_env.setattr(asyncio, "create_subprocess_exec", fake_exec)
        node = MCPClientNode(
            config=NodeConfig(
                params={"transport": "stdio", "command": "desigma-mcp", "tool": "t", "arguments": '{"a": 1}'}
            )
        )
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS

    def test_http_mode_untouched(self, allow_env):
        # 回归: transport 缺省 http — 缺 url 报参数缺失 (原语义)
        node = MCPClientNode(config=NodeConfig(params={"tool": "x"}))
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED
        assert "url" in result.error


class TestDesignContextNode:
    def _node(self, params=None):
        return DesignContextNode(config=NodeConfig(params=params or {}))

    def test_no_workspace_noop(self, monkeypatch):
        monkeypatch.delenv(mcp_stdio.STDIO_ALLOW_ENV, raising=False)

        def _boom(*a, **kw):
            raise AssertionError("no-workspace path must not spawn")

        node = self._node()
        monkeypatch.setattr(node, "_spawn", _boom)
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS
        assert result.data["design_section"] == ""
        assert result.data["design_present"] is False
        assert result.data["exports"] == {}

    def test_deny_without_allowlist(self, monkeypatch):
        monkeypatch.delenv(mcp_stdio.STDIO_ALLOW_ENV, raising=False)
        result = asyncio.run(self._node({"workspace": "/w"}).execute({}))
        assert result.status == NodeStatus.FAILED
        assert "FUSION_COWORK_MCP_STDIO_ALLOW" in result.error

    def test_happy_path_exports(self, allow_env):
        node = self._node({"workspace": "/w", "frame_id": 5})

        async def fake_spawn(command, args, calls, timeout=60.0):
            if calls[0]["tool"] == "desigma_get_document":
                assert calls[1]["tool"] == "desigma_list_nodes"
                return [
                    {"tool": "desigma_get_document", "text": '{"title": "checkout"}', "content": [], "isError": False},
                    {"tool": "desigma_list_nodes", "text": "[]", "content": [], "isError": False},
                ]
            assert [c["arguments"]["node_id"] for c in calls] == [5, 5]
            assert [c["arguments"]["language"] for c in calls] == ["react", "tailwind"]
            return [
                {
                    "tool": "desigma_generate_code",
                    "text": "export function Frame() {}",
                    "content": [],
                    "isError": False,
                },
                {"tool": "desigma_generate_code", "text": ".btn { color: red }", "content": [], "isError": False},
            ]

        allow_env.setattr(node, "_spawn", fake_spawn)
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS
        assert result.data["design_present"] is True
        assert result.data["exports"]["react"] == "export function Frame() {}"
        assert result.data["exports"]["tailwind"] == ".btn { color: red }"
        section = result.data["design_section"]
        assert "## Design contract" in section
        assert "fd-design-lint" in section
        assert "checkout" in section
        assert "export function Frame() {}" in section

    def test_frame_auto_pick_from_list(self, allow_env):
        node = self._node({"workspace": "/w", "frame_id": 0})
        seen_node_ids = []

        async def fake_spawn(command, args, calls, timeout=60.0):
            if calls[0]["tool"] == "desigma_get_document":
                return [
                    {"tool": "desigma_get_document", "text": "doc", "content": [], "isError": False},
                    {
                        "tool": "desigma_list_nodes",
                        "text": json.dumps([{"id": 7, "name": "Home"}]),
                        "content": [],
                        "isError": False,
                    },
                ]
            seen_node_ids.append(calls[0]["arguments"]["node_id"])
            return [{"tool": "desigma_generate_code", "text": "code", "content": [], "isError": False}]

        allow_env.setattr(node, "_spawn", fake_spawn)
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS
        assert seen_node_ids == [7]

    def test_unparseable_list_still_exports_doc(self, allow_env):
        node = self._node({"workspace": "/w", "frame_id": 0})
        codegen_called = []

        async def fake_spawn(command, args, calls, timeout=60.0):
            if calls[0]["tool"] == "desigma_get_document":
                return [
                    {"tool": "desigma_get_document", "text": "doc only", "content": [], "isError": False},
                    {"tool": "desigma_list_nodes", "text": "<not json>", "content": [], "isError": False},
                ]
            codegen_called.append(True)
            return []

        allow_env.setattr(node, "_spawn", fake_spawn)
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.SUCCESS
        assert codegen_called == []
        assert "doc only" in result.data["design_section"]
        assert result.data["exports"] == {}

    def test_spawn_failure_fails_loudly(self, allow_env):
        node = self._node({"workspace": "/w", "frame_id": 1})

        async def fake_spawn(command, args, calls, timeout=60.0):
            raise RuntimeError("server closed stdout early")

        allow_env.setattr(node, "_spawn", fake_spawn)
        result = asyncio.run(node.execute({}))
        assert result.status == NodeStatus.FAILED
        assert "desigma-mcp 导出失败" in result.error


class TestTemplateWiring:
    def _workflow(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        return TemplateManager().template_to_workflow(
            "product_lifecycle", {"slug": "demo", "repo": "o/r", "task_title": "t"}
        )

    def test_design_context_step_wired(self):
        wf = self._workflow()
        assert "n4d" in wf.nodes
        assert wf.nodes["n4d"].name == "design_context"
        edges = {(e.source_id, e.target_id) for e in wf.edges}
        assert ("n2", "n4d") in edges
        assert ("n4d", "n3") in edges

    def test_coder_prompt_carries_design_section_placeholder(self):
        wf = self._workflow()
        prompt = wf.nodes["n7"].config.params["prompt"]
        assert "${step.n4d.design_section}" in prompt

    def test_no_design_defaults_params(self):
        wf = self._workflow()
        assert wf.nodes["n4d"].config.params["workspace"] == ""


class TestNoDesignPipelineUnchanged:
    def test_empty_design_section_yields_byte_identical_prompt(self):
        # 零设计稿红线: design_section 空串拼接后 prompt 与旧版逐字节一致
        from fusion_cowork.nodes.ecosystem.desigma_node import DesignContextNode

        node = DesignContextNode(config=NodeConfig(params={}))
        result = asyncio.run(node.execute({}))
        base_prompt = "Implement the assigned task."
        assert base_prompt + result.data["design_section"] == base_prompt
