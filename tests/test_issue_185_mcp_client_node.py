"""issue #185: mcp_client 节点 — Streamable HTTP initialize/initialized/tools/call 三段式调用外部 MCP 工具。

MockTransport 离线桩: 无真实服务器, 校验协议序列 + 会话头透传 + SSE 响应解析 + 错误路径。
"""

from __future__ import annotations

import json

import httpx

from fusion_cowork.engine._permission_local import HIGH_RISK_NODES, Permission, PermissionManager
from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.nodes.ecosystem.mcp_client_node import MCPClientNode


def _make_node(params=None, inputs=None):
    node = MCPClientNode(config=NodeConfig(params=params or {}))
    return node, inputs or {}


def _handler_sse():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request, body))
        if body.get("method") == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub-mcp", "version": "1.0"},
                },
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "sess-abc-123"},
                text=f"data: {json.dumps(resp)}\n\n",
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body.get("method") == "tools/call":
            assert request.headers.get("Mcp-Session-Id") == "sess-abc-123"
            resp = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": "任务列表为空"}],
                    "isError": False,
                },
            }
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, text=f"data: {json.dumps(resp)}\n\n"
            )
        return httpx.Response(404)

    return handler, calls


class TestMcpClientNode:
    async def test_full_call_sequence_with_session_header(self):
        handler, calls = _handler_sse()
        node, inputs = _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "task_list", "arguments": {}})
        node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        result = await node.execute(inputs)
        assert result.status.value == "success"
        assert result.data["result"] == "任务列表为空"
        methods = [b.get("method") for _, b in calls]
        assert methods == ["initialize", "notifications/initialized", "tools/call"]
        # initialize 请求不带会话头, tools/call 带
        assert "Mcp-Session-Id" not in calls[0][0].headers
        assert calls[2][0].headers.get("Mcp-Session-Id") == "sess-abc-123"

    async def test_json_response_content_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "initialize":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
                )
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
                },
            )

        node, inputs = _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "sms_send", "arguments": {"to": "x"}})
        node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        result = await node.execute(inputs)
        assert result.status.value == "success"
        assert result.data["result"] == "ok"

    async def test_tool_iserror_reported_as_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"content": [{"type": "text", "text": "配额不足"}], "isError": True},
                },
            )

        node, inputs = _make_node({"url": "http://127.0.0.1:11483/mcp", "tool": "sms_send", "arguments": {}})
        node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        result = await node.execute(inputs)
        assert result.status.value == "failed"
        assert "配额不足" in result.error

    async def test_connection_failure_visible(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        node, inputs = _make_node({"url": "http://127.0.0.1:1/mcp", "tool": "task_list"})
        node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        result = await node.execute(inputs)
        assert result.status.value == "failed"
        assert "MCP 连接失败" in result.error

    async def test_missing_params_fail_fast(self):
        node, inputs = _make_node({"tool": "task_list"})
        result = await node.execute(inputs)
        assert result.status.value == "failed"
        node2, _ = _make_node({"url": "ftp://x/mcp", "tool": "t"})
        result2 = await node2.execute({})
        assert result2.status.value == "failed"
        assert "非法 URL" in result2.error

    async def test_extra_headers_forwarded(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "initialize":
                seen["auth"] = request.headers.get("Authorization")
                return httpx.Response(
                    200, headers={"Mcp-Session-Id": "s1"}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
                )
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [], "isError": False}})

        node, inputs = _make_node(
            {
                "url": "http://127.0.0.1:11483/mcp",
                "tool": "t",
                "headers": {"Authorization": "Bearer tok-1"},
            }
        )
        node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        result = await node.execute(inputs)
        assert result.status.value == "success"
        assert seen["auth"] == "Bearer tok-1"


class TestRegistrationAndPermissions:
    def test_registered_via_import_all_nodes(self):
        from fusion_cowork.nodes import import_all_nodes

        import_all_nodes()
        assert NodeRegistry.get("mcp_client") is not None

    def test_high_risk_listing(self):
        assert "mcp_client" in HIGH_RISK_NODES

    async def test_url_scope_permission_matching(self):
        pm = PermissionManager()
        pm.approve("mcp_client", scope="url:http://127.0.0.1:11483/**")
        # 命中授权 URL
        assert await pm.check("mcp_client", "execute", {"url": "http://127.0.0.1:11483/mcp"}) is True
        # 其他服务不放行
        assert await pm.check("mcp_client", "execute", {"url": "http://127.0.0.1:9999/mcp"}) is False
        # 其他节点名不受该规则影响
        assert await pm.check("shell_exec", "execute") is False

    def test_permission_url_scope_unit(self):
        rule = Permission(tool_name="mcp_client", allowed=True, scope="url:http://127.0.0.1:11483/**")
        assert rule.matches("mcp_client", {"url": "http://127.0.0.1:11483/mcp"})
        assert rule.matches("mcp_client", {"url": "http://127.0.0.1:11483"})
        assert not rule.matches("mcp_client", {"url": "http://evil.example.com/mcp"})
        assert not rule.matches("shell_exec", {"url": "http://127.0.0.1:11483/mcp"})

    async def test_high_risk_unapproved_deny(self):
        pm = PermissionManager()
        assert await pm.check("mcp_client", "execute", {"url": "http://127.0.0.1:11483/mcp"}) is False
