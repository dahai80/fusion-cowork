"""issue #211: mcp_client 接受 JSON 字符串 arguments — 解除 mcp_client → mcp_client 连边阻塞。

上游节点输出恒为文本 (data["result"]), 连边 {"source_output": "result", "target_input": "arguments"}
后下游收到字符串。本节点解析 JSON 字符串后继续; 解析失败/非对象仍显式报错。
"""

from __future__ import annotations

import json

import httpx

from fusion_cowork.engine.node import NodeConfig
from fusion_cowork.engine.permission_manager import PermissionLevel, PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes.ecosystem.mcp_client_node import MCPClientNode


def _make_node(params=None):
    return MCPClientNode(config=NodeConfig(params=params or {}))


def _handler(result_text='{"to": "x", "body": "hi"}'):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-211"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"content": [{"type": "text", "text": result_text}], "isError": False},
                },
            )
        return httpx.Response(404)

    return handler, calls


def _call_bodies(calls):
    return [c for c in calls if c.get("method") == "tools/call"]


def _bind(node, handler):
    node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    return node


class TestJsonStringArguments:
    async def test_json_string_arguments_parsed(self):
        handler, calls = _handler("ok")
        node = _bind(_make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2"}), handler)
        result = await node.execute({"arguments": '{"to": "x", "body": "hi"}'})
        assert result.status.value == "success", result.error
        call = _call_bodies(calls)[0]
        assert call["params"]["arguments"] == {"to": "x", "body": "hi"}

    async def test_whitespace_around_json_accepted(self):
        handler, calls = _handler("ok")
        node = _bind(_make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2"}), handler)
        result = await node.execute({"arguments": '  \n {"a": 1} \n '})
        assert result.status.value == "success", result.error
        assert _call_bodies(calls)[0]["params"]["arguments"] == {"a": 1}

    async def test_string_from_params_parsed(self):
        handler, calls = _handler("ok")
        node = _bind(
            _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2", "arguments": '{"k": "v"}'}),
            handler,
        )
        result = await node.execute({})
        assert result.status.value == "success", result.error
        assert _call_bodies(calls)[0]["params"]["arguments"] == {"k": "v"}

    async def test_invalid_json_string_fails_before_http(self):
        handler, calls = _handler("ok")
        node = _bind(_make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2"}), handler)
        result = await node.execute({"arguments": "not json"})
        assert result.status.value == "failed"
        assert "JSON 字符串解析失败" in result.error
        assert calls == []

    async def test_json_string_non_object_rejected(self):
        handler, calls = _handler("ok")
        node = _bind(_make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2"}), handler)
        result = await node.execute({"arguments": "[1, 2]"})
        assert result.status.value == "failed"
        assert result.error == "arguments 须为 JSON 对象"
        assert calls == []

    async def test_non_string_non_dict_rejected(self):
        handler, calls = _handler("ok")
        node = _bind(_make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "t2"}), handler)
        result = await node.execute({"arguments": ["a"]})
        assert result.status.value == "failed"
        assert result.error == "arguments 须为 JSON 对象"
        assert calls == []


class TestChainedMcpClients:
    async def test_result_text_feeds_next_node_arguments(self):
        first_handler, first_calls = _handler('{"to": "+100", "body": "hello"}')
        first = _bind(
            _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "extract"}),
            first_handler,
        )
        second_handler, second_calls = _handler("sent")
        second = _bind(
            _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "send"}),
            second_handler,
        )
        first.id = "a"
        second.id = "b"

        wf = Workflow(name="wf-211")
        wf.add_node(first)
        wf.add_node(second)
        assert wf.connect("a", "b", source_output="result", target_input="arguments")

        engine = WorkflowEngine(permission_manager=PermissionManager(level=PermissionLevel.BYPASS))
        result = await engine.execute(wf, {})

        assert result.status.value == "success", getattr(result, "error", "")
        assert len(_call_bodies(first_calls)) == 1
        sent = _call_bodies(second_calls)[0]["params"]["arguments"]
        assert sent == {"to": "+100", "body": "hello"}
