"""MCP stdio transport layer — stdin/stdout JSON-RPC 2.0.

Fusion-Cowork acts as the MCP server, communicating with Claude Code via stdin/stdout.
Conforms to the Model Context Protocol spec 2024-11-05.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from typing import TYPE_CHECKING, Any, Callable, Dict

from fusion_cowork.observability.trace import get_trace_id

if TYPE_CHECKING:
    from .mcp_server import MCPToolRegistry

logger = logging.getLogger(__name__)

# R-8: unbounded readline → malformed long lines (no \n) cause OOM. Set a line cap; over the cap raises LimitOverrunError.
MAX_LINE_BYTES = 8 * 1024 * 1024  # 8 MiB line cap (a single MCP request, ample headroom)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fusion-cowork"
try:
    from .. import __version__ as SERVER_VERSION
except Exception:
    SERVER_VERSION = "0.0.0"


class StdioTransport:
    """MCP stdio transport — reads JSON-RPC from stdin, writes responses to stdout."""

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
        """Main loop: read requests from stdin, process, write back to stdout."""
        self._running = True
        logger.info("MCP stdio transport started")
        # R-8: length-limited StreamReader; readline beyond the limit raises LimitOverrunError (rather than accumulating unbounded).
        reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
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
                    logger.warning(f"invalid JSON: {e}")
                    await self._send_error(None, -32700, "Parse error")
                    continue

                await self._dispatch(request)

            except TimeoutError:
                continue
            except asyncio.LimitOverrunError:
                # R-8: line exceeded MAX_LINE_BYTES (no \n terminator). Drain the buffer to avoid getting stuck, report and continue.
                logger.error(f"MCP line over the {MAX_LINE_BYTES} byte cap, dropped (possibly malformed input)")
                reader._buffer.clear()
                await self._send_error(None, -32602, "Request too large")
                continue
            except Exception as e:
                logger.error(f"stdio read error: {e}")
                break

        logger.info("MCP stdio transport stopped")

    async def _dispatch(self, request: Dict[str, Any]) -> None:
        """Dispatch a JSON-RPC request to the matching handler."""
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
            # HI-5: trace_id goes into the response, stack trace stays in logs, str(e) never leaked to the MCP client
            trace_id = get_trace_id()
            logger.error(
                "MCP stdio handler exception trace_id=%s method=%s err=%s\n%s",
                trace_id,
                method,
                e,
                traceback.format_exc(),
            )
            if req_id is not None:
                await self._send_error(req_id, -32603, f"Internal error (trace_id={trace_id})")

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP client: {client_info.get('name', 'unknown')} {client_info.get('version', '')}")

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
        logger.info("MCP session initialized")
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
