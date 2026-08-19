"""MCP HTTP+SSE 传输层 — 远程访问 Fusion-Cowork MCP 服务。

基于 FastAPI + SSE (Server-Sent Events)。
客户端通过 POST /mcp 发送 JSON-RPC 请求，通过 GET /sse 接收推送通知。
"""

import asyncio
import json
import logging
from typing import Any, Dict

from .. import __version__ as SERVER_VERSION
from .mcp_server import MCPToolRegistry

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fusion-cowork"


def create_http_app(tool_registry: MCPToolRegistry, event_emitter=None):
    """创建 FastAPI 应用 (MCP HTTP+SSE)。

    Args:
        tool_registry: MCPToolRegistry 实例
        event_emitter: EventEmitter 实例 (可选，用于 SSE 事件推送)

    Returns:
        FastAPI app
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="Fusion-Cowork MCP Server", version=SERVER_VERSION)

    _initialized = False
    _sse_queue: asyncio.Queue = asyncio.Queue()
    _event_emitter = event_emitter

    async def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal _initialized
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP HTTP 客户端: {client_info.get('name', 'unknown')}")
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    async def _handle_initialized(params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal _initialized
        _initialized = True
        logger.info("MCP HTTP 会话已初始化")
        return {}

    async def _handle_tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": tool_registry.list_tools()}

    async def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info(f"MCP HTTP tools/call: {tool_name}")
        return await tool_registry.call_tool(tool_name, arguments)

    async def _handle_ping(params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    _handlers = {
        "initialize": _handle_initialize,
        "initialized": _handle_initialized,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
        "ping": _handle_ping,
    }

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP JSON-RPC 2.0 端点。"""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        method = body.get("method", "")
        req_id = body.get("id")
        params = body.get("params", {})

        handler = _handlers.get(method)
        if not handler:
            if req_id is not None:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Method not found"}}
            )

        if method not in ("initialize", "initialized", "ping") and not _initialized:
            if req_id is not None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": "Server not initialized"}}
                )
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32002, "message": "Not initialized"}})

        try:
            result = await handler(params)
            if req_id is not None:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
            return JSONResponse({"jsonrpc": "2.0", "result": result})
        except Exception as e:
            logger.error(f"MCP HTTP 处理 {method} 异常: {e}")
            if req_id is not None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"Internal error: {e}"}},
                    status_code=500,
                )
            return JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Internal error"}},
                status_code=500,
            )

    @app.post("/rpc")
    async def rpc_endpoint(request: Request):
        """JSON-RPC 2.0 端点 — 托管 plugins/* 方法 (issue #48)。

        委托给 fusion-plugins-ecosystem.MCPHandler, 供 fusion-studio 插件生态面板调用。
        仅路由 plugins/* 方法, 其余返回 -32601。
        """
        from .rpc_bridge import dispatch_rpc

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        response = await dispatch_rpc(body)
        status = 200
        if isinstance(response, dict) and "error" in response:
            status = 500
        return JSONResponse(response, status_code=status)

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        """SSE 推送通道 — 订阅 EventEmitter 事件流。"""

        async def event_stream():
            if _event_emitter:
                sub_id, queue = _event_emitter.subscribe()
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=30)
                            yield event.to_sse()
                        except TimeoutError:
                            yield {"event": "ping", "data": ""}
                finally:
                    _event_emitter.unsubscribe(sub_id)
            else:
                while True:
                    try:
                        data = await asyncio.wait_for(_sse_queue.get(), timeout=30)
                        yield {"event": "message", "data": json.dumps(data, ensure_ascii=False)}
                    except TimeoutError:
                        yield {"event": "ping", "data": ""}

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/health")
    async def health():
        return {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION}

    return app
