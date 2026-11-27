"""MCP HTTP+SSE transport — remote access to the Fusion-Cowork MCP service.

Built on FastAPI + SSE (Server-Sent Events).
Clients send JSON-RPC requests via POST /mcp and receive push notifications via GET /sse.
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

# MD-1: 1MiB request body cap, prevents unbounded-body OOM (uvicorn.Config has no such param, implemented as ASGI middleware)
_MAX_BODY_BYTES = 1024 * 1024


class _BodySizeLimitMiddleware:
    """ASGI middleware: requests exceeding _MAX_BODY_BYTES get a 413 (Content Too Large).

    Transparently proxies attributes to the inner app (FastAPI), so app.title / app.routes etc. stay reachable from outside.
    """

    def __init__(self, app, max_bytes: int = _MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cl = 0
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    cl = int(v)
                except ValueError:
                    cl = 0
                break
        if cl and cl > self.max_bytes:
            await send(
                {"type": "http.response.start", "status": 413, "headers": [[b"content-type", b"application/json"]]}
            )
            await send({"type": "http.response.body", "body": b'{"error":"request body too large"}'})
            return
        await self.app(scope, receive, send)


def _apply_security_middleware(app):
    """Stage 6: compose security middleware — inject tenant_id (JWT) first, then rate limit, then body cap.

    Without FUSION_RATE_LIMIT configured → unlimited rate-limit passthrough (existing behavior unchanged).
    """
    from ..security.rate_limit import FastAPIRateLimitMiddleware, get_default_rate_limiter

    limiter = get_default_rate_limiter()
    wrapped = FastAPIRateLimitMiddleware(app, limiter)
    return _BodySizeLimitMiddleware(wrapped)


def _apply_identity_middleware(app, exempt_paths: frozenset[str]) -> None:
    """issue #88: when fusion-identity is enabled → install the fusion_core tenant middleware + cowork bridge.

    Call before _apply_security_middleware (add_middleware needs the FastAPI object).
    Disabled → no-op (zero behavior change). require_jwt=False: MCP initialize carries no token; the actual token is enforced by _auth_denied.
    """
    try:
        from fusion_cowork.auth.identity import get_identity_client, is_identity_enabled

        if not is_identity_enabled():
            return
        client = get_identity_client()
        if client is None:
            return
    except Exception as e:
        logger.warning(f"mcp http identity middleware install failed, skipping: {e}")
        return
    from fusion_cowork.auth.identity import make_verify_jwt_callback
    from fusion_cowork.tenant import (
        LOCAL_USER,
        reset_current_tenant,
        reset_current_user,
        set_current_tenant,
        set_current_user,
    )

    try:
        from fusion_core.tenant import current, install_tenant_middleware
    except ImportError as e:
        logger.warning(f"fusion_core.tenant unavailable, mcp identity middleware skipped: {e}")
        return

    install_tenant_middleware(
        app,
        exempt_paths=exempt_paths,
        verify_jwt=make_verify_jwt_callback(client),
        require_jwt=False,
    )

    @app.middleware("http")
    async def _cowork_tenant_bridge(request, call_next):
        tctx = current()
        t_tok = set_current_tenant(tctx.tenant_id) if tctx else set_current_tenant("default")
        u_tok = set_current_user(tctx.user_id or LOCAL_USER) if tctx else set_current_user(LOCAL_USER)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(t_tok)
            reset_current_user(u_tok)

    logger.info("mcp http installed fusion-identity tenant middleware + cowork bridge")


def _load_mcp_auth_token() -> Optional[str]:
    """Read config mcp.auth_token; None when unset (auth disabled)."""
    from ..config_center import ConfigCenter

    token = ConfigCenter.get_instance().get("mcp.auth_token")
    if not token or not isinstance(token, str):
        return None
    return token


def _auth_denied(request, token: Optional[str]):
    """Validate Authorization: Bearer <token>; with a token configured, missing/wrong token returns a 401 response, unset returns None.

    Stage 2: JWT active (env FUSION_JWT_SECRET/FUSION_JWKS_URL) → a valid JWT is also accepted.
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    bearer = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    # Stage 2: JWT first — active + valid passes (no static token needed)
    if bearer:
        try:
            from fusion_cowork.auth import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active:
                principal = verifier.verify_token(bearer)
                if principal is not None:
                    # Stage 6: inject tenant_id into request.state for per-tenant metering by the rate-limit middleware.
                    try:
                        request.state.tenant_id = getattr(principal, "tenant_id", "") or "anonymous"
                    except Exception:
                        pass
                    return None
        except Exception as e:
            logger.warning(f"MCP HTTP JWT verification error: {e}")
    if not token:
        return None
    if not bearer or bearer != token:
        logger.warning(f"MCP HTTP authentication failed: {request.client.host if request.client else '?'}")
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
            status_code=401,
        )
    return None


def create_http_app(tool_registry: MCPToolRegistry, event_emitter=None):
    """Create the FastAPI app (MCP HTTP+SSE).

    Args:
        tool_registry: MCPToolRegistry instance
        event_emitter: EventEmitter instance (optional, for SSE event push)

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
        logger.info("MCP HTTP authentication enabled: mcp.auth_token Bearer check")

    async def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal _initialized
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP HTTP client: {client_info.get('name', 'unknown')}")
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    async def _handle_initialized(params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal _initialized
        _initialized = True
        logger.info("MCP HTTP session initialized")
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
        """MCP JSON-RPC 2.0 endpoint."""
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

        # MD-1: JSON-RPC batch (array) explicitly rejected with -32600
        if isinstance(body, list):
            logger.warning("MCP HTTP rejected batch request (%d elements)", len(body))
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
            # HI-5: trace_id goes into the response, stack stays in logs, str(e) never leaks to the client
            trace_id = get_trace_id()
            logger.error(
                "MCP HTTP handler error trace_id=%s method=%s err=%s\n%s", trace_id, method, e, traceback.format_exc()
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
        """JSON-RPC 2.0 endpoint — hosts plugins/* methods (issue #48).

        Delegates to fusion-plugins-ecosystem.MCPHandler for the fusion-studio plugin ecosystem panel.
        Routes only plugins/* methods; everything else returns -32601.
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

        # MD-1: JSON-RPC batch explicitly rejected with -32600 (plugins/* does not support batch)
        if isinstance(body, list):
            logger.warning("MCP /rpc rejected batch request (%d elements)", len(body))
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
        """SSE push channel — subscribe to the EventEmitter event stream."""
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

    _apply_identity_middleware(app, frozenset({"/mcp", "/sse", "/health", "/docs", "/openapi.json"}))
    return _apply_security_middleware(app)


# ---- P2-9: MCP Streamable HTTP (2025-03-26 spec) ----
# Single endpoint POST/DELETE/GET /mcp: the response may be JSON or an SSE stream (server decides).
# Sessions are identified by the Mcp-Session-Id header; client Accept: application/json, text/event-stream.

STREAMABLE_PROTOCOL_VERSION = "2025-03-26"


def create_streamable_app(tool_registry: MCPToolRegistry, event_emitter=None):
    """Create the FastAPI app (MCP Streamable HTTP, 2025-03-26).

    Single /mcp endpoint:
    - POST: client sends JSON-RPC requests; the response may be application/json or text/event-stream (SSE).
    - DELETE: terminate a session (Mcp-Session-Id).
    - GET:  server push stream (optional, kept for compatibility).
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="Fusion-Cowork MCP Streamable Server", version=SERVER_VERSION)

    _sessions: Dict[str, Dict[str, Any]] = {}
    _event_emitter = event_emitter
    _auth_token = _load_mcp_auth_token()
    if _auth_token:
        logger.info("MCP Streamable authentication enabled: mcp.auth_token Bearer check")
    # R-1: session cap + TTL, prevents _sessions from only growing (a client that never DELETEs stays forever, memory leak)
    _MAX_SESSIONS = 200
    _SESSION_TTL = 3600  # seconds; idle sessions past the timeout are swept

    def _sweep_sessions() -> None:
        """R-1: sweep timed-out sessions + evict oldest over the cap, prevents unbounded _sessions growth."""
        now = time.time()
        expired = [sid for sid, s in _sessions.items() if now - s.get("last_seen", now) > _SESSION_TTL]
        for sid in expired:
            _sessions.pop(sid, None)
            logger.info(f"MCP Streamable session expired, reclaimed: {sid}")
        if len(_sessions) > _MAX_SESSIONS:
            oldest = sorted(_sessions.items(), key=lambda kv: kv[1].get("last_seen", 0))
            for sid, _ in oldest[: len(_sessions) - _MAX_SESSIONS]:
                _sessions.pop(sid, None)
                logger.info(f"MCP Streamable session evicted over cap: {sid}")

    async def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP Streamable client: {client_info.get('name', 'unknown')}")
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

        # MD-1: JSON-RPC batch explicitly rejected with -32600 (streamable single-request semantics)
        if isinstance(body, list):
            logger.warning("MCP Streamable rejected batch request (%d elements)", len(body))
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests not supported"}},
                status_code=400,
            )

        # R-1: sweep timed-out/over-cap sessions per request, prevents grow-only
        _sweep_sessions()

        # initialize must create a session
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
            logger.info(f"MCP Streamable session initialized: {session_id}")
            # notification has no id → 202
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

            # Server decides: tools/call goes through the SSE stream when the client accepts text/event-stream
            if method == "tools/call" and _is_stream_requested(request):
                return _stream_response(req_id, result, session_id, headers)

            if req_id is not None:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result}, headers=headers)
            return JSONResponse({"jsonrpc": "2.0", "result": {}}, headers=headers, status_code=202)
        except Exception as e:
            # HI-5: trace_id goes into the response, stack stays in logs
            trace_id = get_trace_id()
            logger.error(
                "MCP Streamable handler error trace_id=%s method=%s err=%s\n%s",
                trace_id,
                method,
                e,
                traceback.format_exc(),
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
        """Build the SSE stream response — emit the final JSON-RPC result first, then (optionally) append events."""

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
            logger.info(f"MCP Streamable session terminated: {session_id}")
            return JSONResponse({"status": "terminated"}, status_code=200)
        return JSONResponse({"error": "session not found"}, status_code=404)

    @app.get("/mcp")
    async def streamable_get(request: Request):
        """GET /mcp — server push stream (requires a valid session)."""
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

    _apply_identity_middleware(app, frozenset({"/mcp", "/health", "/docs", "/openapi.json"}))
    return _apply_security_middleware(app)
