"""MCP stdio 传输层 — stdin/stdout JSON-RPC 2.0。

Fusion-Cowork 作为 MCP server，通过 stdin/stdout 与 Claude Code 通信。
符合 Model Context Protocol 规范 2024-11-05。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any, Callable, Dict

if TYPE_CHECKING:
    from .mcp_server import MCPToolRegistry

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fusion-cowork"
try:
    from .. import __version__ as SERVER_VERSION
except Exception:
    SERVER_VERSION = "0.0.0"


class StdioTransport:
    """MCP stdio 传输 — 从 stdin 读取 JSON-RPC，向 stdout 写入响应。"""

    def __init__(self, tool_registry: MCPToolRegistry):
        self._registry = tool_registry
        self._running = False
        self._initialized = False
        self._request_handlers: Dict[str, Callable] = {
            "initialize": self._handle_initialize,
            "initialized": self._handle_initialized,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "ping": self._handle_ping,
            "shutdown": self._handle_shutdown,
        }

    async def run(self) -> None:
        """主循环: 从 stdin 读取请求，处理，写回 stdout。"""
        self._running = True
        logger.info("MCP stdio 传输启动")
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"无效 JSON: {e}")
                    await self._send_error(None, -32700, "Parse error")
                    continue

                await self._dispatch(request)

            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"stdio 读取异常: {e}")
                break

        logger.info("MCP stdio 传输停止")

    async def _dispatch(self, request: Dict[str, Any]) -> None:
        """分发 JSON-RPC 请求到对应处理器。"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        handler = self._request_handlers.get(method)
        if not handler:
            if req_id is not None:
                await self._send_error(req_id, -32601, f"Method not found: {method}")
            return

        if method not in ("initialize", "initialized", "ping") and not self._initialized:
            if req_id is not None:
                await self._send_error(req_id, -32002, "Server not initialized")
            return

        try:
            result = await handler(params)
            if req_id is not None:
                await self._send_result(req_id, result)
        except Exception as e:
            logger.error(f"处理 {method} 异常: {e}")
            if req_id is not None:
                await self._send_error(req_id, -32603, f"Internal error: {e}")

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP 客户端: {client_info.get('name', 'unknown')} {client_info.get('version', '')}")

        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": True},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    async def _handle_initialized(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._initialized = True
        logger.info("MCP 会话已初始化")
        return {}

    async def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tools = self._registry.list_tools()
        return {"tools": tools}

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info(f"MCP tools/call: {tool_name}")

        result = await self._registry.call_tool(tool_name, arguments)
        return result

    async def _handle_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def _handle_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._running = False
        return {}

    async def _send_result(self, req_id: Any, result: Any) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }
        await self._write(response)

    async def _send_error(self, req_id: Any, code: int, message: str) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
        await self._write(response)

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write(notification)

    async def _write(self, data: Dict[str, Any]) -> None:
        line = json.dumps(data, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
