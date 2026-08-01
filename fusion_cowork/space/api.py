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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


def create_space_api(
    space_svc,
    member_svc,
    chat_svc,
    kb_svc,
    event_emitter=None,
    agent_runtime=None,
):

    app = FastAPI(title="Fusion-Cowork Space API", version="0.8.0")
    _event_emitter = event_emitter

    # ── Space CRUD ──

    @app.post("/spaces")
    async def create_space(request: Request):
        body = await request.json()
        sp = await space_svc.create(
            name=body.get("name", ""),
            description=body.get("description", ""),
            owner_id=body.get("owner_id", ""),
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
        body = await request.json()
        m = await member_svc.add_direct(
            space_id=space_id,
            user_id=body["user_id"],
            display_name=body.get("display_name", ""),
            operator_id=body.get("operator_id", ""),
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
    async def list_messages(space_id: str, limit: int = 100, offset: int = 0):
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

    return app
