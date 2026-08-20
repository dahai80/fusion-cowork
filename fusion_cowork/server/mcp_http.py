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


# ---- P2-9: MCP Streamable HTTP (2025-03-26 spec) ----
# 单一端点 POST/DELETE/GET /mcp: 响应可为 JSON 或 SSE 流 (服务器决定)。
# 会话由 Mcp-Session-Id 头标识; 客户端 Accept: application/json, text/event-stream。

STREAMABLE_PROTOCOL_VERSION = "2025-03-26"


def create_streamable_app(tool_registry: MCPToolRegistry, event_emitter=None):
    """创建 FastAPI 应用 (MCP Streamable HTTP, 2025-03-26)。

    单一 /mcp 端点:
    - POST: 客户端发 JSON-RPC 请求; 响应可为 application/json 或 text/event-stream (SSE)。
    - DELETE: 终止会话 (Mcp-Session-Id)。
    - GET:  服务端推送流 (可选, 保持兼容)。
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="Fusion-Cowork MCP Streamable Server", version=SERVER_VERSION)

    _sessions: Dict[str, Dict[str, Any]] = {}
    _event_emitter = event_emitter

    async def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP Streamable 客户端: {client_info.get('name', 'unknown')}")
        return {
            "protocolVersion": STREAMABLE_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    async def _handle_tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": tool_registry.list_tools()}

    async def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info(f"MCP Streamable tools/call: {tool_name}")
        return await tool_registry.call_tool(tool_name, arguments)

    async def _handle_ping(params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    _handlers = {
        "initialize": _handle_initialize,
        "notifications/initialized": _handle_ping,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
        "ping": _handle_ping,
    }

    def _is_stream_requested(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "text/event-stream" in accept

    def _new_session_id() -> str:
        import uuid

        return f"mcp-{uuid.uuid4().hex[:16]}"

    @app.post("/mcp")
    async def streamable_endpoint(request: Request):
        session_id = request.headers.get("mcp-session-id", "")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # initialize 必须建会话
        if body.get("method") == "initialize":
            session_id = _new_session_id()
            _sessions[session_id] = {"initialized": False}
        elif session_id not in _sessions:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "Bad Request: missing or invalid session"}},
                status_code=400,
            )

        sess = _sessions[session_id]
        method = body.get("method", "")
        req_id = body.get("id")
        params = body.get("params", {})

        if method == "notifications/initialized":
            sess["initialized"] = True
            logger.info(f"MCP Streamable 会话初始化完成: {session_id}")
            # notification 无 id → 202
            return JSONResponse({"jsonrpc": "2.0", "result": {}}, status_code=202)

        handler = _handlers.get(method)
        if not handler:
            if req_id is not None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}},
                    status_code=200,
                )
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Method not found"}}, status_code=200)

        if method not in ("initialize", "ping") and not sess.get("initialized"):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": "Server not initialized"}},
                status_code=200,
            )

        try:
            result = await handler(params)
            headers = {"mcp-session-id": session_id}

            # 服务器决定: tools/call 若客户端接受 text/event-stream 则走 SSE 流
            if method == "tools/call" and _is_stream_requested(request):
                return _stream_response(req_id, result, session_id, headers)

            if req_id is not None:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result}, headers=headers)
            return JSONResponse({"jsonrpc": "2.0", "result": {}}, headers=headers, status_code=202)
        except Exception as e:
            logger.error(f"MCP Streamable 处理 {method} 异常: {e}")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"Internal error: {e}"}},
                status_code=500,
            )

    def _stream_response(req_id, result, session_id, headers):
        """构造 SSE 流响应 — 先吐最终 JSON-RPC 结果, 再 (可选) 追加事件。"""

        async def gen():
            payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)
            yield f"event: message\ndata: {payload}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.delete("/mcp")
    async def streamable_delete(request: Request):
        session_id = request.headers.get("mcp-session-id", "")
        if session_id in _sessions:
            del _sessions[session_id]
            logger.info(f"MCP Streamable 会话终止: {session_id}")
            return JSONResponse({"status": "terminated"}, status_code=200)
        return JSONResponse({"error": "session not found"}, status_code=404)

    @app.get("/mcp")
    async def streamable_get(request: Request):
        """GET /mcp — 服务端推送流 (需有效会话)。"""
        session_id = request.headers.get("mcp-session-id", "")
        if session_id not in _sessions:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if not _event_emitter:
            return JSONResponse({"error": "no event source"}, status_code=406)

        sub_id, queue = _event_emitter.subscribe()

        async def event_stream():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30)
                        yield f"event: message\ndata: {event.to_sse()}\n\n"
                    except TimeoutError:
                        yield "event: ping\ndata: \n\n"
            finally:
                _event_emitter.unsubscribe(sub_id)

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"mcp-session-id": session_id})

    @app.get("/health")
    async def health():
        return {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION, "protocol": "streamable-2025-03-26"}

    return app
