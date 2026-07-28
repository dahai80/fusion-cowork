"""M1 里程碑测试 — MCP stdio 传输, Desk RPC, Agent 真实执行。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import fusion_desk.nodes.macos  # noqa: F401
import fusion_desk.nodes.ai  # noqa: F401
import fusion_desk.nodes.io  # noqa: F401
import fusion_desk.nodes.logic  # noqa: F401
import fusion_desk.nodes.tools  # noqa: F401

from fusion_desk.engine.node import NodeRegistry, NodeConfig


# ── MCPToolRegistry ──

class TestMCPToolRegistry:

    def test_register_and_list_tools(self):
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        registry.register_tools()
        tools = registry.list_tools()
        assert len(tools) >= 14
        names = [t["name"] for t in tools]
        assert "read_file" in names
        assert "run_terminal" in names
        assert "desktop_cleanup" in names

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        registry.register_tools()
        result = await registry.call_tool("nonexistent", {})
        assert result.get("isError") is True

    def test_input_schema_uses_inputSchema(self):
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        registry.register_tools()
        for tool in registry.list_tools():
            assert "inputSchema" in tool, f"{tool['name']} 缺少 inputSchema"


# ── StdioTransport ──

class TestStdioTransport:

    def test_handler_registration(self):
        from fusion_desk.server.mcp_transport import StdioTransport
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        transport = StdioTransport(registry)
        assert "initialize" in transport._request_handlers
        assert "tools/list" in transport._request_handlers
        assert "tools/call" in transport._request_handlers
        assert "ping" in transport._request_handlers

    @pytest.mark.asyncio
    async def test_handle_initialize(self):
        from fusion_desk.server.mcp_transport import StdioTransport, MCP_PROTOCOL_VERSION
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        transport = StdioTransport(registry)
        result = await transport._handle_initialize({"clientInfo": {"name": "test"}})
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "fusion-desk"
        assert "tools" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_handle_tools_list(self):
        from fusion_desk.server.mcp_transport import StdioTransport
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        registry.register_tools()
        transport = StdioTransport(registry)
        result = await transport._handle_tools_list({})
        assert "tools" in result
        assert len(result["tools"]) >= 14

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found(self):
        from fusion_desk.server.mcp_transport import StdioTransport
        from fusion_desk.server.mcp_server import MCPToolRegistry
        registry = MCPToolRegistry()
        transport = StdioTransport(registry)
        with patch.object(transport, "_send_error", new_callable=AsyncMock) as mock_err:
            await transport._dispatch({"method": "nonexistent", "id": 1, "params": {}})
            mock_err.assert_called_once()


# ── MCPServer 门面 ──

class TestMCPServer:

    def test_compatible_get_tools_list(self):
        from fusion_desk.server.mcp_server import MCPServer
        server = MCPServer()
        server._registry.register_tools()
        tools = server.get_tools_list()
        assert len(tools) >= 14

    @pytest.mark.asyncio
    async def test_compatible_handle_tool_call(self):
        from fusion_desk.server.mcp_server import MCPServer
        server = MCPServer()
        server._registry.register_tools()
        result = await server.handle_tool_call("nonexistent_tool", {})
        assert result.get("isError") is True


# ── DeskRPCServer ──

class TestDeskRPCServer:

    def test_handler_registration(self):
        from fusion_desk.server.desk_rpc import DeskRPCServer
        rpc = DeskRPCServer()
        assert "desk.health" in rpc._handlers
        assert "desk.nodes.list" in rpc._handlers
        assert "desk.nodes.execute" in rpc._handlers
        assert "desk.workflow.list" in rpc._handlers
        assert "desk.agent.list" in rpc._handlers
        assert "desk.mlx.status" in rpc._handlers
        assert "desk.system.info" in rpc._handlers

    @pytest.mark.asyncio
    async def test_handle_health(self):
        from fusion_desk.server.desk_rpc import DeskRPCServer
        rpc = DeskRPCServer()
        result = await rpc._handle_health({})
        assert result["status"] == "ok"
        assert result["service"] == "fusion-desk"

    @pytest.mark.asyncio
    async def test_handle_nodes_list(self):
        from fusion_desk.server.desk_rpc import DeskRPCServer
        rpc = DeskRPCServer()
        result = await rpc._handle_nodes_list({})
        assert "nodes" in result
        assert result["count"] > 0

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found(self):
        from fusion_desk.server.desk_rpc import DeskRPCServer
        rpc = DeskRPCServer()
        response = await rpc._dispatch({"method": "desk.nonexistent", "id": 1, "params": {}})
        assert "error" in response
        assert response["error"]["code"] == -32601


# ── Agent 执行器 ──

class TestExecutors:

    @pytest.mark.asyncio
    async def test_node_executor_missing_node_name(self):
        from fusion_desk.orchestrator.executors import NodeExecutor
        ex = NodeExecutor()
        result = await ex({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_node_executor_unknown_node(self):
        from fusion_desk.orchestrator.executors import NodeExecutor
        ex = NodeExecutor()
        result = await ex({"node_name": "nonexistent_node_xyz", "node_params": {}})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_workflow_executor_missing_params(self):
        from fusion_desk.orchestrator.executors import WorkflowExecutor
        ex = WorkflowExecutor()
        result = await ex({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_shell_executor_missing_command(self):
        from fusion_desk.orchestrator.executors import ShellExecutor
        ex = ShellExecutor()
        result = await ex({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_shell_executor_echo(self):
        from fusion_desk.orchestrator.executors import ShellExecutor
        ex = ShellExecutor()
        result = await ex({"command": "echo hello", "timeout": 10})
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_default_executors_keys(self):
        from fusion_desk.orchestrator.executors import DEFAULT_EXECUTORS
        assert "executor_node" in DEFAULT_EXECUTORS
        assert "executor_workflow" in DEFAULT_EXECUTORS
        assert "executor_mlx" in DEFAULT_EXECUTORS
        assert "executor_shell" in DEFAULT_EXECUTORS


# ── AgentOrchestrator 增强 ──

class TestAgentOrchestratorEnhanced:

    def test_register_default_agents(self):
        from fusion_desk.orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        orch.register_default_agents()
        assert len(orch._agents) >= 7
        assert "planner" in orch._agents
        assert "executor_node" in orch._agents
        assert "analyzer" in orch._agents

    def test_register_default_executors(self):
        from fusion_desk.orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        orch.register_default_agents()
        assert "executor_node" in orch._executors
        assert "executor_shell" in orch._executors

    @pytest.mark.asyncio
    async def test_submit_task(self):
        from fusion_desk.orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        orch.register_default_agents()
        task_id = await orch.submit_task("test task", {"command": "echo test", "timeout": 10})
        assert task_id.startswith("task_")
        assert task_id in orch._tasks


# ── AgentMessageBus ──

class TestAgentMessageBus:

    def test_subscribe(self):
        from fusion_desk.orchestrator.comm import AgentMessageBus
        bus = AgentMessageBus()
        q = bus.subscribe("test_topic")
        assert "test_topic" in bus._subscribers
        assert len(bus._subscribers["test_topic"]) == 1

    @pytest.mark.asyncio
    async def test_publish(self):
        from fusion_desk.orchestrator.comm import AgentMessageBus
        bus = AgentMessageBus()
        q = bus.subscribe("test_topic")
        msg_id = await bus.publish("test_topic", "sender_a", {"key": "value"})
        msg = q.get_nowait()
        assert msg.sender == "sender_a"
        assert msg.payload == {"key": "value"}

    @pytest.mark.asyncio
    async def test_send_point_to_point(self):
        from fusion_desk.orchestrator.comm import AgentMessageBus
        bus = AgentMessageBus()
        q = bus.subscribe("inbox:receiver_b")
        msg_id = await bus.send("sender_a", "receiver_b", {"data": 42})
        msg = q.get_nowait()
        assert msg.sender == "sender_a"
        assert msg.receiver == "receiver_b"

    def test_history(self):
        from fusion_desk.orchestrator.comm import AgentMessageBus
        bus = AgentMessageBus()
        bus._history.append(
            type("Msg", (), {"topic": "test", "msg_id": "1"})()
        )
        assert len(bus.get_history("test")) == 1


# ── MCP HTTP App ──

class TestMCPHttpApp:

    def test_create_http_app(self):
        pytest.importorskip("fastapi", reason="需要 [web] 依赖")
        from fusion_desk.server.mcp_server import MCPToolRegistry
        from fusion_desk.server.mcp_http import create_http_app
        registry = MCPToolRegistry()
        registry.register_tools()
        app = create_http_app(registry)
        assert app.title == "Fusion-Desk MCP Server"
