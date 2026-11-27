"""Collaborative Space REST API — FastAPI routes + SSE event stream.

Endpoints:
- POST   /spaces                     Create space
- GET    /spaces                     List spaces
- GET    /spaces/{space_id}          Get space
- PUT    /spaces/{space_id}          Update space
- DELETE /spaces/{space_id}          Delete space
- POST   /spaces/{id}/members        Add member
- GET    /spaces/{id}/members        List members
- POST   /spaces/{id}/messages       Send message
- GET    /spaces/{id}/messages       List messages
- GET    /spaces/{id}/stream         SSE event stream
- POST   /spaces/{id}/kb/bind        Bind knowledge base
- POST   /spaces/{id}/kb/upload      Upload document
- GET    /spaces/{id}/kb/search      Search knowledge base
- GET    /spaces/{id}/kb/status      Knowledge base status
- GET    /spaces/{id}/agents         List agents
- POST   /spaces/{id}/agents         Add agent
- GET    /spaces/{id}/agents/{aid}   Get agent
- DELETE /spaces/{id}/agents/{aid}   Delete agent
- POST   /spaces/{id}/agents/call    Call agent
- POST   /spaces/{id}/agents/relay   Multi-agent relay
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from fusion_cowork.tenant import (
    DEFAULT_TENANT,
    LOCAL_USER,
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)

from .artifact import ConflictError

logger = logging.getLogger(__name__)


def create_space_api(
    space_svc,
    member_svc,
    chat_svc,
    kb_svc,
    artifact_svc=None,
    notification_svc=None,
    event_emitter=None,
    agent_runtime=None,
    presence_manager=None,
    space_store=None,
    auth_token: Optional[str] = None,
    principal_resolver=None,
    collab_hub=None,
):
    """Collaborative Space REST API.

    A-5 (0825 audit): identity fields are no longer trusted from the request body —
    owner_id/user_id/operator_id always come from the connection-level trusted
    identity returned by principal_resolver(request) (default local_user). Identity
    fields in the body are ignored, preventing CR-5-style spoofing/IDOR. When
    auth_token is configured, all endpoints verify `Authorization: Bearer <token>`.
    Read endpoints such as list_messages / get_space add SpacePermission.check
    (view_artifact / send_message); non-members are rejected.

    A-9 (0825 audit): CollabHub (WS transport layer) is injected by the caller
    (server/cli) via collab_hub; the space domain no longer reverse-imports
    ..server.collab_ws — the layering boundary stays one-way (server consumes space).
    """

    app = FastAPI(title="Fusion-Cowork Space API", version="0.8.0")
    _event_emitter = event_emitter
    app.state.event_emitter = event_emitter

    # Stage 1: inject tenant_id into a contextvar per request; downstream store.resolve_tenant_id(None) picks it up automatically.
    # Stage 2: once JWT is wired, _resolve_principal returns the JWT claim tenant_id.
    @app.middleware("http")
    async def _tenant_middleware(request: Request, call_next):
        tid = _resolve_tenant(request)
        t_tok = set_current_tenant(tid)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(t_tok)

    from .permission import SpacePermission
    from .presence import PresenceManager

    _presence = presence_manager or PresenceManager(event_emitter=event_emitter)
    # A-5: space_store prefers explicit injection; falls back to member_svc._store (services share one store)
    _space_store = space_store or getattr(member_svc, "_store", None)
    _auth_token = auth_token
    _principal_resolver = principal_resolver

    def _resolve_principal(request: Request) -> TenantPrincipal:
        """A-5: extract a trusted principal — never read identity fields from the request body. Returns a TenantPrincipal (with tenant_id).

        - principal_resolver configured → caller-defined (e.g. parse JWT/header).
        - Stage 2: otherwise, if a JWT verifier is active (env FUSION_JWT_SECRET/FUSION_JWKS_URL) →
          verify from `Authorization: Bearer <jwt>` and extract tenant_id/user_id claims.
        - Otherwise: if auth_token is configured, take it from the `X-Principal` header
          (trusted after token verification); no token in local single-user mode →
          local_user / default tenant.
        owner_id/user_id in the body are never trusted (CR-5 anti-IDOR).
        """
        if _principal_resolver:
            resolved = _principal_resolver(request)
            if isinstance(resolved, TenantPrincipal):
                return resolved
            if isinstance(resolved, str):
                return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=resolved or LOCAL_USER)
            return TenantPrincipal()
        # Stage 2: JWT (env-configured active) — verify from Authorization Bearer
        try:
            from fusion_cowork.auth import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active:
                authz = request.headers.get("Authorization", "") or request.headers.get("authorization", "")
                token = authz[len("Bearer ") :] if authz.startswith("Bearer ") else ""
                if token:
                    jwt_principal = verifier.verify_token(token)
                    if jwt_principal is not None:
                        return jwt_principal
        except Exception as e:
            logger.warning(f"Space API JWT verification error, falling back to static identity: {e}")
        if _auth_token:
            uid = request.headers.get("X-Principal", "") or LOCAL_USER
            return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=uid)
        return TenantPrincipal()

    def _resolve_tenant(request: Request) -> str:
        """Stage 1: extract tenant_id (Stage 2 fully wires JWT; this stage defaults to default)."""
        return _resolve_principal(request).tenant_id

    async def _check_auth(request: Request) -> Optional[JSONResponse]:
        """A-5: if auth_token is configured, verify Bearer; missing/wrong returns 401. Stage 2: JWT also passes."""
        # Stage 2: JWT active → pass on successful verification (tenant_id extracted by _resolve_principal)
        try:
            from fusion_cowork.auth import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active:
                authz = request.headers.get("Authorization", "") or request.headers.get("authorization", "")
                token = authz[len("Bearer ") :] if authz.startswith("Bearer ") else ""
                if token and verifier.verify_token(token) is not None:
                    return None
                # JWT active but token missing/invalid: if a static token is also configured, continue with static verification, otherwise 401
                if not _auth_token:
                    logger.warning("Space API auth failed: JWT invalid or missing")
                    return JSONResponse({"error": "auth failed: invalid token"}, status_code=401)
        except Exception as e:
            logger.warning(f"Space API JWT verification error: {e}")
        if not _auth_token:
            return None
        authz = request.headers.get("Authorization", "")
        token = authz[len("Bearer ") :] if authz.startswith("Bearer ") else ""
        if token != _auth_token:
            logger.warning("Space API auth failed: invalid Bearer token")
            return JSONResponse({"error": "auth failed: invalid token"}, status_code=401)
        return None

    async def _require_access(
        space_id: str, principal: str, action: str, tenant_id: Optional[str] = None
    ) -> Optional[JSONResponse]:
        """A-5: IDOR guard — verify the principal's membership in the space + permissions.
        Owner passes fully; non-member gets 403; members are checked against the role matrix for action. Stage 1: pass tenant_id to the store."""
        if not _space_store or not space_id:
            return None
        tid = tenant_id or DEFAULT_TENANT
        space = await _space_store.get_space(space_id, tenant_id=tid)
        if not space:
            return JSONResponse({"error": "space not found"}, status_code=404)
        owner_id = getattr(space, "owner_id", "") if space else ""
        if principal and principal == owner_id:
            return None
        member = await _space_store.get_member(space_id, principal, tenant_id=tid)
        if member is None:
            logger.warning(f"Space API IDOR rejected: principal={principal} is not a member of space {space_id} tenant={tid}")
            return JSONResponse({"error": "no access to this space"}, status_code=403)
        if action:
            perm = SpacePermission(_space_store)
            if not await perm.check(space_id, principal, action):
                return JSONResponse({"error": f"insufficient permission: missing {action}"}, status_code=403)
        return None

    # ── Space CRUD ──

    @app.post("/spaces")
    async def create_space(request: Request):
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        body = await request.json()
        # A-5: owner_id comes from the trusted principal, not from body (CR-5 anti-IDOR)
        principal = _resolve_principal(request)
        sp = await space_svc.create(
            name=body.get("name", ""),
            description=body.get("description", ""),
            owner_id=principal.user_id,
            tenant_id=principal.tenant_id,
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
        # A-5: operator comes from the trusted principal, not body.operator_id (CR-5 anti-spoofing).
        # body.user_id is the member being added (not an identity assertion) and role is a role — both are operation targets, not subjects.
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        operator_principal = _resolve_principal(request)
        access_err = await _require_access(
            space_id, operator_principal.user_id, "manage_members", tenant_id=operator_principal.tenant_id
        )
        if access_err:
            return access_err
        body = await request.json()
        m = await member_svc.add_direct(
            space_id=space_id,
            user_id=body["user_id"],
            display_name=body.get("display_name", ""),
            operator_id=operator_principal.user_id,
            role=body.get("role"),
        )
        return JSONResponse(m.to_dict(), status_code=201)

    @app.get("/spaces/{space_id}/members")
    async def list_members(space_id: str):
        members = await member_svc.list_members(space_id)
        return JSONResponse([m.to_dict() for m in members])

    @app.delete("/spaces/{space_id}/members/{user_id}")
    async def remove_member(space_id: str, user_id: str, request: Request):
        # A-5: operator comes from the trusted principal (not query/body identity fields, CR-5 anti-spoofing).
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        operator_principal = _resolve_principal(request)
        access_err = await _require_access(
            space_id, operator_principal.user_id, "manage_members", tenant_id=operator_principal.tenant_id
        )
        if access_err:
            return access_err
        await member_svc.remove(space_id, user_id, operator_id=operator_principal.user_id)
        return JSONResponse({"status": "removed"})

    @app.put("/spaces/{space_id}/members/{user_id}")
    async def update_member_role(space_id: str, user_id: str, request: Request):
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        operator_principal = _resolve_principal(request)
        access_err = await _require_access(
            space_id, operator_principal.user_id, "manage_members", tenant_id=operator_principal.tenant_id
        )
        if access_err:
            return access_err
        body = await request.json()
        if not body.get("role"):
            return JSONResponse({"error": "role is required"}, status_code=400)
        try:
            m = await member_svc.update_role(space_id, user_id, body["role"], operator_id=operator_principal.user_id)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        if not m:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(m.to_dict())

    # ── Permission Matrix (W16) ──

    @app.get("/spaces/{space_id}/permissions")
    async def get_permission_matrix(space_id: str, request: Request):
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        from .permission import SpacePermission

        matrix = {role: SpacePermission.get_permissions_for_role(role) for role in SpacePermission.list_roles()}
        return JSONResponse({"roles": SpacePermission.list_roles(), "matrix": matrix})

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
            parent_msg_id=body.get("parent_msg_id"),
            model=body.get("model", ""),
        )
        return JSONResponse(msg.to_dict(), status_code=201)

    @app.get("/spaces/{space_id}/messages")
    async def list_messages(space_id: str, request: Request, limit: int = 100, offset: int = 0):
        # A-5: list_messages has no user_id/permission.check → IDOR, any user could read any space's messages.
        # Verify the connection-level principal's membership (read → empty-string action only checks membership, any role may read).
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        access_err = await _require_access(space_id, principal.user_id, "", tenant_id=principal.tenant_id)
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
                    # B2: immediate ready frame — frontend uses it to turn off polling fallback
                    yield f"event: ready\ndata: {json.dumps({'space_id': space_id}, ensure_ascii=False)}\n\n"
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

    # ── Artifacts (P2 W10/W12: exposed on the web surface — list/create/get) ──

    @app.get("/spaces/{space_id}/artifacts")
    async def list_artifacts(space_id: str, request: Request, artifact_type: str = ""):
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        try:
            items = await artifact_svc.list_artifacts(
                space_id, principal.user_id, artifact_type=artifact_type, tenant_id=principal.tenant_id
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        return JSONResponse(items)

    @app.post("/spaces/{space_id}/artifacts")
    async def create_artifact(space_id: str, request: Request):
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        body = await request.json()
        try:
            art = await artifact_svc.create_artifact(
                space_id,
                principal.user_id,
                name=str(body.get("name") or ""),
                artifact_type=str(body.get("artifact_type") or "document"),
                content=str(body.get("content") or ""),
                metadata=body.get("metadata"),
                tenant_id=principal.tenant_id,
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        return JSONResponse(art, status_code=201)

    @app.get("/spaces/{space_id}/artifacts/{artifact_id}")
    async def get_artifact(space_id: str, artifact_id: str, request: Request):
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        try:
            art = await artifact_svc.get_artifact(
                space_id, artifact_id, principal.user_id, tenant_id=principal.tenant_id
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        if not art:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(art)

    @app.post("/spaces/{space_id}/artifacts/{artifact_id}/review")
    async def review_artifact(space_id: str, artifact_id: str, request: Request):
        # W9 delivery review: approved=true→approve / false→reject (owner/admin, state stored in metadata.review)
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        body = await request.json()
        try:
            res = await artifact_svc.review_artifact(
                space_id,
                artifact_id,
                principal.user_id,
                approved=bool(body.get("approved")),
                note=str(body.get("note") or ""),
                tenant_id=principal.tenant_id,
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse(res)

    # ── issue #149: in-place AI revision of artifacts — selection → AI rewrite → block-level accept/reject → write back ──
    @app.post("/spaces/{space_id}/artifacts/{artifact_id}/ai_revise")
    async def ai_revise_artifact(space_id: str, artifact_id: str, request: Request):
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        body = await request.json()
        selection = str(body.get("selection") or "").strip()
        instruction = str(body.get("instruction") or "").strip()
        if not selection:
            return JSONResponse({"error": "selection is required"}, status_code=400)
        mlx = getattr(chat_svc, "mlx", None) if chat_svc else None
        if mlx is None:
            return JSONResponse({"error": "model client not available"}, status_code=503)
        try:
            art = await artifact_svc.get_artifact(
                space_id, artifact_id, principal.user_id, tenant_id=principal.tenant_id
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        if not art:
            return JSONResponse({"error": "not found"}, status_code=404)
        content = str(art.get("content") or "")
        if selection not in content:
            return JSONResponse({"error": "selection does not match artifact content, please re-select"}, status_code=400)

        model = str(body.get("model") or "").strip()
        if not model:
            try:
                from ..ai.nl_parser import _pick_chat_model

                model = _pick_chat_model(await mlx.list_models())
            except Exception as e:
                logger.warning(f"ai_revise automatic model selection failed: {e}")
        if not model:
            return JSONResponse({"error": "no chat model available"}, status_code=503)

        messages = [
            {"role": "system", "content": "You are a text revision assistant. Output only the revised text itself; no explanations, no code block wrapping."},
            {
                "role": "user",
                "content": f"Revise the selected text per these requirements:\nRequirement: {instruction or 'improve wording while preserving the original meaning'}\n\nOriginal text:\n{selection}",
            },
        ]
        try:
            resp = await mlx.chat(model=model, messages=messages, temperature=0.2, max_tokens=2048)
        except Exception as e:
            logger.error(f"ai_revise model call failed: {e}")
            return JSONResponse({"error": f"model call failed: {e}"}, status_code=502)
        revised = str(resp.content or "").strip()
        # The model occasionally wraps output in ``` — deterministic unwrapping, not model-driven
        if revised.startswith("```"):
            revised = revised.split("\n", 1)[-1]
            if revised.endswith("```"):
                revised = revised[:-3].rstrip()
        from ..engine.ast_diff import text_diff_blocks

        blocks = text_diff_blocks(selection, revised)
        logger.info(f"ai_revise artifact={artifact_id} user={principal.user_id} model={model} blocks={len(blocks)}")
        return JSONResponse({"selection": selection, "revised": revised, "diff_blocks": blocks, "model": model})

    @app.post("/spaces/{space_id}/artifacts/{artifact_id}/apply_revision")
    async def apply_revision_artifact(space_id: str, artifact_id: str, request: Request):
        if not artifact_svc:
            return JSONResponse({"error": "artifact service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        body = await request.json()
        selection = str(body.get("selection") or "")
        revised = str(body.get("revised") or "")
        model = str(body.get("model") or "")
        accepted = body.get("accepted_blocks")
        # #142 alignment: optional block-level partial acceptance — rebuild revised keeping only accepted blocks
        if isinstance(accepted, list):
            from ..engine.ast_diff import apply_block_selection

            revised = apply_block_selection(selection, revised, accepted)
        if not selection or not revised:
            return JSONResponse({"error": "selection/revised are required"}, status_code=400)
        try:
            art = await artifact_svc.get_artifact(
                space_id, artifact_id, principal.user_id, tenant_id=principal.tenant_id
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        if not art:
            return JSONResponse({"error": "not found"}, status_code=404)
        content = str(art.get("content") or "")
        if selection not in content:
            return JSONResponse({"error": "selection does not match artifact content, please refresh and retry"}, status_code=400)
        new_content = content.replace(selection, revised, 1)
        metadata = art.get("metadata") if isinstance(art.get("metadata"), dict) else {}
        history = list(metadata.get("revision_history") or [])
        history.append(
            {
                "ts": datetime.now().isoformat(),
                "user_id": principal.user_id,
                "model": model,
                "selection": selection[:200],
                "revised": revised[:200],
            }
        )
        try:
            await artifact_svc.update_artifact(
                space_id,
                artifact_id,
                principal.user_id,
                content=new_content,
                metadata={**metadata, "revision_history": history},
                tenant_id=principal.tenant_id,
            )
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ConflictError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        logger.info(f"apply_revision artifact={artifact_id} user={principal.user_id} v+1")
        return JSONResponse({"id": artifact_id, "applied": True, "revision_count": len(history)})

    # ── Notifications (W15) ──

    @app.get("/notifications")
    async def list_notifications(request: Request, unread_only: bool = False):
        if not notification_svc:
            return JSONResponse({"error": "notification service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        items = await notification_svc.list_notifications(
            principal.user_id, unread_only=unread_only, tenant_id=principal.tenant_id
        )
        return JSONResponse(items)

    @app.post("/notifications/{notification_id}/read")
    async def mark_notification_read(notification_id: str, request: Request):
        if not notification_svc:
            return JSONResponse({"error": "notification service not available"}, status_code=503)
        auth_err = await _check_auth(request)
        if auth_err:
            return auth_err
        principal = _resolve_principal(request)
        ok = await notification_svc.mark_read(notification_id, tenant_id=principal.tenant_id)
        return JSONResponse({"marked": ok})

    # ── Health ──

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "fusion-cowork-space"}

    # ── WebSocket bidirectional collaboration channel (A-9: CollabHub injected by caller, no reverse import of server) ──

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
                logger.warning(f"WS error: {e}")
                await _collab_hub.leave(websocket)
    else:
        # A-9: CollabHub not injected (e.g. standalone tests of the space domain) → WS endpoint degrades to refuse connection, app startup does not crash.
        @app.websocket("/spaces/{space_id}/ws")
        async def collab_ws(websocket: WebSocket, space_id: str):
            await websocket.accept()
            await websocket.close(code=1011, reason="WS collaboration channel not configured (collab_hub not injected)")

    # issue #88: fusion-identity enabled → install_tenant_middleware (fusion_core) + cowork bridge.
    # The bridge maps fusion_core.tenant.current() → cowork contextvar (dual ContextVars, 28+ downstream readers use cowork).
    # WS paths bypass HTTP middleware and still go through fallback.verify_any_token (identity-aware seam).
    _install_identity_middleware(app)
    return app


def _install_identity_middleware(app) -> None:
    """issue #88: when identity is enabled, install the fusion_core tenant middleware + cowork contextvar bridge.

    Disabled → no-op (keeps the original _tenant_middleware, zero behavior change).
    """
    try:
        from fusion_cowork.auth.identity import get_identity_client, is_identity_enabled

        if not is_identity_enabled():
            return
        client = get_identity_client()
        if client is None:
            return
    except Exception as e:
        logger.warning(f"space api identity middleware install failed, skipping: {e}")
        return
    from fusion_cowork.auth.identity import make_verify_jwt_callback
    from fusion_cowork.tenant import (
        reset_current_tenant,
        reset_current_user,
        set_current_tenant,
        set_current_user,
    )

    try:
        from fusion_core.tenant import current, install_tenant_middleware
    except ImportError as e:
        logger.warning(f"fusion_core.tenant unavailable, skipping identity middleware: {e}")
        return

    install_tenant_middleware(
        app,
        exempt_paths=frozenset({"/health", "/docs", "/openapi.json", "/redoc"}),
        verify_jwt=make_verify_jwt_callback(client),
        require_jwt=True,
    )

    @app.middleware("http")
    async def _cowork_tenant_bridge(request: Request, call_next):
        tctx = current()
        t_tok = set_current_tenant(tctx.tenant_id) if tctx else set_current_tenant(DEFAULT_TENANT)
        u_tok = set_current_user(tctx.user_id or LOCAL_USER) if tctx else set_current_user(LOCAL_USER)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(t_tok)
            reset_current_user(u_tok)

    logger.info("space api installed fusion-identity tenant middleware + cowork bridge")
