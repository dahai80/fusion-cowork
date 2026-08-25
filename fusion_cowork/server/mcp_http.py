"""MCP HTTP+SSE 传输层 — 远程访问 Fusion-Cowork MCP 服务。

基于 FastAPI + SSE (Server-Sent Events)。
客户端通过 POST /mcp 发送 JSON-RPC 请求，通过 GET /sse 接收推送通知。
"""

import asyncio
import json
import logging
import time
import traceback
from typing import Any, Dict, Optional

from fusion_cowork.observability.trace import get_trace_id

from .. import __version__ as SERVER_VERSION
from .mcp_server import MCPToolRegistry

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fusion-cowork"


def _load_mcp_auth_token() -> Optional[str]:
    """读取 config mcp.auth_token; 未配则 None (不启用认证)。"""
    from ..config_center import ConfigCenter

    token = ConfigCenter.get_instance().get("mcp.auth_token")
    if not token or not isinstance(token, str):
        return None
    return token


def _auth_denied(request, token: Optional[str]):
    """校验 Authorization: Bearer <token>; 配了 token 则缺/错返 401 响应, 未配返 None。

    Stage 2: JWT active (env FUSION_JWT_SECRET/FUSION_JWKS_URL) → 有效 JWT 也放行。
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    bearer = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    # Stage 2: JWT 优先 — active 且有效即放行 (无需静态 token)
    if bearer:
        try:
            from fusion_cowork.auth import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active and verifier.verify_token(bearer) is not None:
                return None
        except Exception as e:
            logger.warning(f"MCP HTTP JWT 校验异常: {e}")
    if not token:
        return None
    if not bearer or bearer != token:
        logger.warning(f"MCP HTTP 认证失败: {request.client.host if request.client else '?'}")
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
            status_code=401,
        )
    return None


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
    _auth_token = _load_mcp_auth_token()
    if _auth_token:
        logger.info("MCP HTTP 认证已启用: mcp.auth_token Bearer 校验")

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
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # MD-1: JSON-RPC batch (数组) 显式拒 -32600
        if isinstance(body, list):
            logger.warning("MCP HTTP 拒绝 batch 请求 (%d 元素)", len(body))
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests not supported"}},
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
            # HI-5: trace_id 入响应, 栈仅日志, 不泄 str(e) 给客户端
            trace_id = get_trace_id()
            logger.error(
                "MCP HTTP 处理异常 trace_id=%s method=%s err=%s\n%s", trace_id, method, e, traceback.format_exc()
            )
            if req_id is not None:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": "Internal error", "data": {"trace_id": trace_id}},
                    },
                    status_code=500,
                )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Internal error", "data": {"trace_id": trace_id}},
                },
                status_code=500,
            )

    @app.post("/rpc")
    async def rpc_endpoint(request: Request):
        """JSON-RPC 2.0 端点 — 托管 plugins/* 方法 (issue #48)。

        委托给 fusion-plugins-ecosystem.MCPHandler, 供 fusion-studio 插件生态面板调用。
        仅路由 plugins/* 方法, 其余返回 -32601。
        """
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied
        from .rpc_bridge import dispatch_rpc

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # MD-1: JSON-RPC batch 显式拒 -32600 (plugins/* 不支持 batch)
        if isinstance(body, list):
            logger.warning("MCP /rpc 拒绝 batch 请求 (%d 元素)", len(body))
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests not supported"}},
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
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied

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
        from ..observability.health import run_health

        result = await run_health(None)
        result["server"] = SERVER_NAME
        result["version"] = SERVER_VERSION
        return result

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
    _auth_token = _load_mcp_auth_token()
    if _auth_token:
        logger.info("MCP Streamable 认证已启用: mcp.auth_token Bearer 校验")
    # R-1: 会话上限 + TTL, 防 _sessions 只增不删 (客户端不 DELETE 则永驻, 内存泄漏)
    _MAX_SESSIONS = 200
    _SESSION_TTL = 3600  # 秒; 超时未活动会话被清扫

    def _sweep_sessions() -> None:
        """R-1: 清扫超时会话 + 超上限淘汰最旧, 防 _sessions 无界增长。"""
        now = time.time()
        expired = [sid for sid, s in _sessions.items() if now - s.get("last_seen", now) > _SESSION_TTL]
        for sid in expired:
            _sessions.pop(sid, None)
            logger.info(f"MCP Streamable 会话超时回收: {sid}")
        if len(_sessions) > _MAX_SESSIONS:
            oldest = sorted(_sessions.items(), key=lambda kv: kv[1].get("last_seen", 0))
            for sid, _ in oldest[: len(_sessions) - _MAX_SESSIONS]:
                _sessions.pop(sid, None)
                logger.info(f"MCP Streamable 会话超上限淘汰: {sid}")

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
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied
        session_id = request.headers.get("mcp-session-id", "")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        # MD-1: JSON-RPC batch 显式拒 -32600 (streamable 单请求语义)
        if isinstance(body, list):
            logger.warning("MCP Streamable 拒绝 batch 请求 (%d 元素)", len(body))
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests not supported"}},
                status_code=400,
            )

        # R-1: 每请求清扫超时/超上限会话, 防只增不删
        _sweep_sessions()

        # initialize 必须建会话
        if body.get("method") == "initialize":
            session_id = _new_session_id()
            _sessions[session_id] = {"initialized": False, "last_seen": time.time()}
        elif session_id not in _sessions:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32000, "message": "Bad Request: missing or invalid session"},
                },
                status_code=400,
            )

        sess = _sessions[session_id]
        sess["last_seen"] = time.time()
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
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    },
                    status_code=200,
                )
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Method not found"}},
                status_code=200,
            )

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
            # HI-5: trace_id 入响应, 栈仅日志
            trace_id = get_trace_id()
            logger.error(
                "MCP Streamable 处理异常 trace_id=%s method=%s err=%s\n%s", trace_id, method, e, traceback.format_exc()
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": "Internal error", "data": {"trace_id": trace_id}},
                },
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
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied
        session_id = request.headers.get("mcp-session-id", "")
        if session_id in _sessions:
            del _sessions[session_id]
            logger.info(f"MCP Streamable 会话终止: {session_id}")
            return JSONResponse({"status": "terminated"}, status_code=200)
        return JSONResponse({"error": "session not found"}, status_code=404)

    @app.get("/mcp")
    async def streamable_get(request: Request):
        """GET /mcp — 服务端推送流 (需有效会话)。"""
        denied = _auth_denied(request, _auth_token)
        if denied is not None:
            return denied
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
        from ..observability.health import run_health

        result = await run_health(None)
        result["server"] = SERVER_NAME
        result["version"] = SERVER_VERSION
        result["protocol"] = "streamable-2025-03-26"
        return result

    return app
