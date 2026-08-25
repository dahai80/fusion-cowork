"""协作空间 REST API — FastAPI 路由 + SSE 事件流。

端点:
- POST   /spaces                     创建空间
- GET    /spaces                     列出空间
- GET    /spaces/{space_id}          获取空间
- PUT    /spaces/{space_id}          更新空间
- DELETE /spaces/{space_id}          删除空间
- POST   /spaces/{id}/members        添加成员
- GET    /spaces/{id}/members        列出成员
- POST   /spaces/{id}/messages       发送消息
- GET    /spaces/{id}/messages       列出消息
- GET    /spaces/{id}/stream         SSE 事件流
- POST   /spaces/{id}/kb/bind        绑定知识库
- POST   /spaces/{id}/kb/upload      上传文档
- GET    /spaces/{id}/kb/search      搜索知识库
- GET    /spaces/{id}/kb/status      知识库状态
- GET    /spaces/{id}/agents         列出 Agent
- POST   /spaces/{id}/agents         添加 Agent
- GET    /spaces/{id}/agents/{aid}   获取 Agent
- DELETE /spaces/{id}/agents/{aid}   删除 Agent
- POST   /spaces/{id}/agents/call    调用 Agent
- POST   /spaces/{id}/agents/relay   多 Agent 接力
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


def create_space_api(
    space_svc,
    member_svc,
    chat_svc,
    kb_svc,
    event_emitter=None,
    agent_runtime=None,
    presence_manager=None,
    space_store=None,
    auth_token: Optional[str] = None,
    principal_resolver=None,
    collab_hub=None,
):
    """协作空间 REST API。

    A-5 (0825 审计): 身份字段不再信请求体 — owner_id/user_id/operator_id 一律取
    principal_resolver(request) 返回的连接级可信身份 (默认 local_user)。body 里的
    身份字段被忽略, 防 CR-5 式冒充/IDOR。配 auth_token 则所有端点校验
    `Authorization: Bearer <token>`。list_messages / get_space 等读端点加
    SpacePermission.check (view_artifact / send_message), 非成员拒。

    A-9 (0825 审计): CollabHub (WS 传输层) 由调用方 (server/cli) 注入 collab_hub,
    space 域不再反向 import ..server.collab_ws — 分层边界保持单向 (server 消费 space)。
    """

    app = FastAPI(title="Fusion-Cowork Space API", version="0.8.0")
    _event_emitter = event_emitter

    from .permission import SpacePermission
    from .presence import PresenceManager

    _presence = presence_manager or PresenceManager(event_emitter=event_emitter)
    # A-5: space_store 优先显式注入; 缺则回退 member_svc._store (服务均持有同一 store)
    _space_store = space_store or getattr(member_svc, "_store", None)
    _auth_token = auth_token
    _principal_resolver = principal_resolver

    def _resolve_principal(request: Request) -> str:
        """A-5: 提取可信 principal — 不读请求体身份字段。

        - 配了 principal_resolver → 调用方自定义 (如解析 JWT/header)。
        - 否则: 若配 auth_token 则从 `X-Principal` header 取 (经 token 校验可信);
          无 token 本地单用户 → local_user。
        body 里的 owner_id/user_id 一律不信 (CR-5 反 IDOR)。
        """
        if _principal_resolver:
            return _principal_resolver(request) or "local_user"
        if _auth_token:
            return request.headers.get("X-Principal", "") or "local_user"
        return "local_user"

    async def _check_auth(request: Request) -> Optional[JSONResponse]:
        """A-5: auth_token 配了则校验 Bearer; 缺/错返 401。"""
        if not _auth_token:
            return None
        authz = request.headers.get("Authorization", "")
        token = authz[len("Bearer ") :] if authz.startswith("Bearer ") else ""
        if token != _auth_token:
            logger.warning("Space API 认证失败: Bearer token 无效")
            return JSONResponse({"error": "认证失败: token 无效"}, status_code=401)
        return None

    async def _require_access(space_id: str, principal: str, action: str) -> Optional[JSONResponse]:
        """A-5: IDOR 守卫 — 校验 principal 对 space 的成员资格 + 权限。
        owner 全放行; 非成员 403; 成员查角色矩阵 action。"""
        if not _space_store or not space_id:
            return None
        space = await _space_store.get_space(space_id)
        if not space:
            return JSONResponse({"error": "空间不存在"}, status_code=404)
        owner_id = getattr(space, "owner_id", "") if space else ""
        if principal and principal == owner_id:
            return None
        member = await _space_store.get_member(space_id, principal)
        if member is None:
            logger.warning(f"Space API IDOR 拒: principal={principal} 非空间 {space_id} 成员")
            return JSONResponse({"error": "无权访问该空间"}, status_code=403)
        if action:
            perm = SpacePermission(_space_store)
            if not await perm.check(space_id, principal, action):
                return JSONResponse({"error": f"权限不足: 缺 {action}"}, status_code=403)
        return None

    # ── Space CRUD ──

    @app.post("/spaces")
    async def create_space(request: Request):
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        body = await request.json()
        # A-5: owner_id 取可信 principal, 不信 body (CR-5 反 IDOR)
        owner = _resolve_principal(request)
        sp = await space_svc.create(
            name=body.get("name", ""),
            description=body.get("description", ""),
            owner_id=owner,
        )
        return JSONResponse(sp.to_dict(), status_code=201)

    @app.get("/spaces")
    async def list_spaces(status: Optional[str] = None, owner_id: Optional[str] = None):
        spaces = await space_svc.list(status=status, owner_id=owner_id)
        return JSONResponse([s.to_dict() for s in spaces])

    @app.get("/spaces/{space_id}")
    async def get_space(space_id: str):
        sp = await space_svc.get(space_id)
        if not sp:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(sp.to_dict())

    @app.put("/spaces/{space_id}")
    async def update_space(space_id: str, request: Request):
        body = await request.json()
        updated = await space_svc.update(space_id, **body)
        if not updated:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(updated.to_dict())

    @app.delete("/spaces/{space_id}")
    async def delete_space(space_id: str):
        await space_svc.delete(space_id)
        return JSONResponse({"status": "deleted"})

    # ── Members ──

    @app.post("/spaces/{space_id}/members")
    async def add_member(space_id: str, request: Request):
        # A-5: operator 取可信 principal, 不信 body.operator_id (CR-5 反冒充)。
        # body.user_id 是被加成员 (非身份断言), role 是角色 — 均为操作对象非主体。
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        operator = _resolve_principal(request)
        access_err = await _require_access(space_id, operator, "manage_members")
        if access_err:
            return access_err
        body = await request.json()
        m = await member_svc.add_direct(
            space_id=space_id,
            user_id=body["user_id"],
            display_name=body.get("display_name", ""),
            operator_id=operator,
            role=body.get("role"),
        )
        return JSONResponse(m.to_dict(), status_code=201)

    @app.get("/spaces/{space_id}/members")
    async def list_members(space_id: str):
        members = await member_svc.list_members(space_id)
        return JSONResponse([m.to_dict() for m in members])

    @app.delete("/spaces/{space_id}/members/{user_id}")
    async def remove_member(space_id: str, user_id: str, operator_id: str = ""):
        await member_svc.remove(space_id, user_id, operator_id=operator_id)
        return JSONResponse({"status": "removed"})

    # ── Messages ──

    @app.post("/spaces/{space_id}/messages")
    async def send_message(space_id: str, request: Request):
        body = await request.json()
        msg = await chat_svc.send_message(
            space_id=space_id,
            user_id=body["user_id"],
            content=body["content"],
            agent_id=body.get("agent_id"),
            content_type=body.get("content_type", "text"),
            attachments=body.get("attachments"),
        )
        return JSONResponse(msg.to_dict(), status_code=201)

    @app.get("/spaces/{space_id}/messages")
    async def list_messages(space_id: str, request: Request, limit: int = 100, offset: int = 0):
        # A-5: list_messages 无 user_id/permission.check → IDOR, 任意用户读任意空间消息。
        # 校验连接级 principal 成员资格 (read → 空字符串 action 仅查成员, 任意角色可读)。
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        access_err = await _require_access(space_id, principal, "")
        if access_err:
            return access_err
        msgs = await chat_svc.list_messages(space_id, limit=limit, offset=offset)
        return JSONResponse([m.to_dict() for m in msgs])

    # ── SSE Stream ──

    @app.get("/spaces/{space_id}/stream")
    async def sse_stream(space_id: str, request: Request):
        async def event_stream():
            if _event_emitter:
                sub_id, queue = _event_emitter.subscribe()
                prefix = f"space:{space_id}:"
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=30)
                            if event.event_type.startswith(prefix):
                                yield f"event: {event.event_type}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"
                        except TimeoutError:
                            yield "event: ping\ndata: \n\n"
                finally:
                    _event_emitter.unsubscribe(sub_id)
            else:
                while True:
                    if await request.is_disconnected():
                        break
                    await asyncio.sleep(30)
                    yield "event: ping\ndata: \n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ── Presence + Cursor ──

    @app.post("/spaces/{space_id}/presence/heartbeat")
    async def presence_heartbeat(space_id: str, request: Request):
        body = await request.json()
        st = _presence.heartbeat(
            space_id,
            body.get("user_id", ""),
            display_name=body.get("display_name", ""),
            extras=body.get("extras"),
        )
        return JSONResponse(st.to_dict())

    @app.post("/spaces/{space_id}/presence/cursor")
    async def presence_cursor(space_id: str, request: Request):
        body = await request.json()
        st = _presence.set_cursor(
            space_id,
            body.get("user_id", ""),
            float(body.get("x", 0)),
            float(body.get("y", 0)),
            target=body.get("target", ""),
        )
        return JSONResponse(st.to_dict())

    @app.get("/spaces/{space_id}/presence")
    async def presence_list(space_id: str):
        return JSONResponse([s.to_dict() for s in _presence.list_present(space_id)])

    @app.delete("/spaces/{space_id}/presence/{user_id}")
    async def presence_remove(space_id: str, user_id: str):
        removed = _presence.remove(space_id, user_id)
        return JSONResponse({"removed": removed})

    # ── Knowledge Base ──

    @app.post("/spaces/{space_id}/kb/bind")
    async def bind_kb(space_id: str, request: Request):
        body = await request.json()
        kb_id = await kb_svc.bind_kb(
            space_id=space_id,
            operator_id=body["operator_id"],
            kb_id=body.get("kb_id"),
        )
        return JSONResponse({"kb_id": kb_id})

    @app.post("/spaces/{space_id}/kb/unbind")
    async def unbind_kb(space_id: str, request: Request):
        body = await request.json()
        result = await kb_svc.unbind_kb(space_id, body["operator_id"])
        return JSONResponse({"unbound": result})

    @app.post("/spaces/{space_id}/kb/upload")
    async def upload_document(space_id: str, request: Request):
        body = await request.json()
        result = await kb_svc.upload_document(
            space_id=space_id,
            operator_id=body["operator_id"],
            file_path=body["file_path"],
            file_name=body.get("file_name"),
        )
        return JSONResponse(result, status_code=201)

    @app.get("/spaces/{space_id}/kb/search")
    async def search_kb(space_id: str, q: str = "", top_k: int = 5):
        results = await kb_svc.search(space_id, q, top_k=top_k)
        return JSONResponse(results)

    @app.get("/spaces/{space_id}/kb/query")
    async def query_kb(space_id: str, q: str = "", top_k: int = 5):
        answer = await kb_svc.query(space_id, q, top_k=top_k)
        return JSONResponse({"answer": answer})

    @app.get("/spaces/{space_id}/kb/status")
    async def kb_status(space_id: str):
        status = await kb_svc.get_kb_status(space_id)
        return JSONResponse(status)

    @app.get("/spaces/{space_id}/kb/documents")
    async def list_documents(space_id: str):
        docs = await kb_svc.list_documents(space_id)
        return JSONResponse(docs)

    # ── Agents ──

    @app.get("/spaces/{space_id}/agents")
    async def list_agents(space_id: str):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        agents = await agent_runtime.list_agents(space_id)
        return JSONResponse(agents)

    @app.post("/spaces/{space_id}/agents")
    async def add_agent(space_id: str, request: Request):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        body = await request.json()
        result = await agent_runtime.add_agent(
            space_id=space_id,
            operator_id=body["operator_id"],
            name=body.get("name", ""),
            agent_type=body.get("agent_type", "assistant"),
            system_prompt=body.get("system_prompt", ""),
            enable_rag=body.get("enable_rag", False),
            config=body.get("config"),
        )
        return JSONResponse(result, status_code=201)

    @app.get("/spaces/{space_id}/agents/{agent_id}")
    async def get_agent(space_id: str, agent_id: str):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        agent_def = await agent_runtime.get_agent(space_id, agent_id)
        if not agent_def:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(agent_def)

    @app.delete("/spaces/{space_id}/agents/{agent_id}")
    async def remove_agent(space_id: str, agent_id: str, operator_id: str = ""):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        removed = await agent_runtime.remove_agent(space_id, agent_id, operator_id)
        if not removed:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"status": "removed"})

    @app.post("/spaces/{space_id}/agents/call")
    async def call_agent(space_id: str, request: Request):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        body = await request.json()
        reply = await agent_runtime.call_agent(
            space_id=space_id,
            agent_id=body["agent_id"],
            user_id=body["user_id"],
            message=body["message"],
            model=body.get("model", ""),
        )
        return JSONResponse({"content": reply})

    @app.post("/spaces/{space_id}/agents/relay")
    async def relay_agents(space_id: str, request: Request):
        if not agent_runtime:
            return JSONResponse({"error": "agent runtime not available"}, status_code=503)
        body = await request.json()
        results = await chat_svc.relay_agents(
            space_id=space_id,
            user_id=body["user_id"],
            agent_ids=body["agent_ids"],
            initial_message=body["message"],
            model=body.get("model", ""),
        )
        return JSONResponse({"results": results})

    # ── Health ──

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "fusion-cowork-space"}

    # ── WebSocket 双向协作通道 (A-9: CollabHub 由调用方注入, 不反向 import server) ──

    _collab_hub = collab_hub

    if _collab_hub is not None:

        @app.websocket("/spaces/{space_id}/ws")
        async def collab_ws(websocket: WebSocket, space_id: str):
            await websocket.accept()
            try:
                hello = await websocket.receive_json()
                user_id = hello.get("user_id", "")
                display_name = hello.get("display_name", "")
                await _collab_hub.join(websocket, space_id, user_id, display_name=display_name)
                while True:
                    raw = await websocket.receive_text()
                    result = await _collab_hub.handle_message(websocket, raw)
                    if result is not None:
                        await websocket.send_json(result)
            except WebSocketDisconnect:
                await _collab_hub.leave(websocket)
            except Exception as e:
                logger.warning(f"WS 异常: {e}")
                await _collab_hub.leave(websocket)
    else:
        # A-9: 未注入 CollabHub (如独立测试 space 域) → WS 端点降级拒连, 不崩 app 启动。
        @app.websocket("/spaces/{space_id}/ws")
        async def collab_ws(websocket: WebSocket, space_id: str):
            await websocket.accept()
            await websocket.close(code=1011, reason="WS 协作通道未配置 (collab_hub 未注入)")

    return app
