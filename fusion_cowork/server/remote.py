"""远程控制服务 — WebSocket 接入。

允许外部客户端连接运行中的 fusion-cowork 会话，
查看状态、提交工作流、取消任务，事件通过 WS 实时推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RemoteControlServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11439,
        token: Optional[str] = None,
        tls_cert: str = "",
        tls_key: str = "",
    ):
        self.host = host
        self.port = port
        self.token = token
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self._server = None
        self._clients: Dict[str, Any] = {}
        self._running = False
        # 命名会话 attach: session_id -> {client_id, attached_at}
        self._session_attachments: Dict[str, str] = {}

    async def start(self):
        try:
            import websockets
        except ImportError:
            logger.error("websockets 未安装，请运行: pip install websockets")
            raise

        self._running = True
        ssl_context = self._build_ssl_context()
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ssl=ssl_context,
        )
        scheme = "wss" if ssl_context else "ws"
        logger.info(
            f"RemoteControlServer started on {scheme}://{self.host}:{self.port}/control (tls={bool(ssl_context)})"
        )

    def _build_ssl_context(self):
        """构造 TLS ssl_context — P2-10。仅当 tls_cert/tls_key 提供时启用。"""
        if not self.tls_cert or not self.tls_key:
            return None
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=self.tls_cert, keyfile=self.tls_key)
            logger.info(f"RemoteControlServer TLS 已启用: cert={self.tls_cert}")
            return ctx
        except Exception as e:
            logger.error(f"TLS 证书加载失败, 降级明文: {e}")
            return None

    async def stop(self):
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        logger.info("RemoteControlServer stopped")

    async def _handler(self, websocket, path=None):
        client_id = uuid.uuid4().hex[:8]
        self._clients[client_id] = websocket
        logger.info(f"Remote client connected: {client_id}")

        try:
            async for message in websocket:
                try:
                    request = json.loads(message)
                    response = await self._process_request(client_id, request)
                    await websocket.send(json.dumps(response))
                except json.JSONDecodeError as e:
                    await websocket.send(json.dumps({"error": f"Invalid JSON: {e}"}))
                except Exception as e:
                    logger.error(f"Error processing request from {client_id}: {e}")
                    await websocket.send(json.dumps({"error": str(e)}))
        except Exception as e:
            logger.warning(f"Client {client_id} disconnected: {e}")
        finally:
            self._clients.pop(client_id, None)
            logger.info(f"Remote client disconnected: {client_id}")

    async def _process_request(self, client_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        if self.token and not request.get("_authenticated"):
            auth_token = params.get("token") or request.get("token", "")
            if auth_token != self.token:
                return {"id": req_id, "error": "Authentication failed"}

        if method == "status":
            return {"id": req_id, "result": await self._get_status()}
        elif method == "list_nodes":
            return {"id": req_id, "result": self._list_nodes()}
        elif method == "list_templates":
            return {"id": req_id, "result": self._list_templates()}
        elif method == "submit_workflow":
            result = await self._submit_workflow(params)
            return {"id": req_id, "result": result}
        elif method == "run_template":
            result = await self._run_template(params)
            return {"id": req_id, "result": result}
        elif method == "cancel_task":
            result = self._cancel_task(params.get("task_id", ""))
            return {"id": req_id, "result": result}
        elif method == "list_sessions":
            return {"id": req_id, "result": self._list_sessions()}
        elif method == "attach_session":
            result = await self._attach_session(client_id, params.get("session_id", ""))
            return {"id": req_id, "result": result}
        else:
            return {"id": req_id, "error": f"Unknown method: {method}"}

    async def _get_status(self) -> Dict[str, Any]:
        from fusion_cowork.engine.node import NodeRegistry

        nodes = NodeRegistry.list()
        return {
            "status": "running",
            "clients": len(self._clients),
            "registered_nodes": len(nodes),
            "uptime": time.time(),
        }

    def _list_nodes(self) -> List[Dict[str, Any]]:
        from fusion_cowork.engine.node import NodeRegistry

        return NodeRegistry.list()

    def _list_templates(self) -> List[Dict[str, Any]]:
        try:
            from fusion_cowork.templates import TemplateManager

            mgr = TemplateManager()
            templates = mgr.list_templates()
            return [
                {"id": t.get("id", ""), "name": t.get("name", ""), "category": t.get("category", "")} for t in templates
            ]
        except Exception as e:
            logger.warning(f"list_templates failed: {e}")
            return []

    async def _submit_workflow(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from fusion_cowork.engine import Workflow, WorkflowEngine

        workflow_data = params.get("workflow", {})
        try:
            workflow = Workflow.from_dict(workflow_data)
            engine = WorkflowEngine()
            task_id = uuid.uuid4().hex[:12]

            asyncio.create_task(self._run_workflow_background(task_id, engine, workflow, params.get("input_data", {})))

            return {"task_id": task_id, "status": "submitted"}
        except Exception as e:
            logger.error(f"submit_workflow failed: {e}")
            return {"error": str(e)}

    async def _run_workflow_background(self, task_id: str, engine, workflow, input_data: Dict):
        try:
            result = await engine.execute(workflow, input_data)
            await self._broadcast(
                {
                    "event": "task_completed",
                    "task_id": task_id,
                    "status": result.status.value if hasattr(result, "status") else "completed",
                }
            )
        except Exception as e:
            logger.error(f"Workflow {task_id} failed: {e}")
            await self._broadcast(
                {
                    "event": "task_failed",
                    "task_id": task_id,
                    "error": str(e),
                }
            )

    async def _run_template(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from fusion_cowork.templates import TemplateManager

        template_id = params.get("template_id", "")
        try:
            mgr = TemplateManager()
            template = mgr.get_template(template_id)
            if not template:
                return {"error": f"Template not found: {template_id}"}
            task_id = uuid.uuid4().hex[:12]
            return {"task_id": task_id, "status": "submitted", "template": template_id}
        except Exception as e:
            return {"error": str(e)}

    def _cancel_task(self, task_id: str) -> Dict[str, Any]:
        return {"task_id": task_id, "status": "cancel_requested"}

    def _list_sessions(self) -> List[Dict[str, Any]]:
        try:
            from fusion_cowork.engine.session import SessionStore

            store = SessionStore()
            sessions = store.list_sessions()
            return [{"id": s.id, "status": s.status, "workflow_name": s.workflow_name} for s in sessions]
        except Exception as e:
            logger.debug(f"list_sessions failed: {e}")
            return []

    async def _attach_session(self, client_id: str, session_id: str) -> Dict[str, Any]:
        """命名会话 attach — P2-10。客户端绑定指定 session_id, 后续事件推送定向送达。

        返回会话快照 (供客户端重放) + 标记绑定关系。
        """
        if not session_id:
            return {"error": "缺少 session_id"}
        try:
            from fusion_cowork.engine.session import SessionStore

            store = SessionStore()
            session = store.get_session(session_id)
            if not session:
                return {"error": f"会话不存在: {session_id}"}
            self._session_attachments[session_id] = client_id
            logger.info(f"Remote client {client_id} attach 会话: {session_id} (status={session.status})")
            return {
                "attached": True,
                "session_id": session_id,
                "status": session.status,
                "workflow_name": session.workflow_name,
                "steps_snapshot": getattr(session, "steps_snapshot", []),
            }
        except Exception as e:
            logger.error(f"attach_session failed: {e}")
            return {"error": str(e)}

    async def _broadcast(self, message: Dict[str, Any]):
        msg_str = json.dumps(message, ensure_ascii=False)
        disconnected = []
        for cid, ws in self._clients.items():
            try:
                await ws.send(msg_str)
            except Exception:
                disconnected.append(cid)
        for cid in disconnected:
            self._clients.pop(cid, None)


class RemoteControlClient:
    def __init__(self, token: Optional[str] = None, tls_verify: bool = True):
        self.token = token
        self.tls_verify = tls_verify
        self._ws = None

    async def connect(self, url: str = "ws://127.0.0.1:11439/control"):
        try:
            import websockets
        except ImportError:
            logger.error("websockets 未安装，请运行: pip install websockets")
            raise
        connect_kwargs: Dict[str, Any] = {}
        if url.startswith("wss://") and not self.tls_verify:
            # 仅自签名开发证书场景; 生产应将 CA 加入信任库, 保留 tls_verify=True
            import ssl

            logger.warning("RemoteControlClient 禁用 TLS 校验 (仅限自签名开发证书)")
            connect_kwargs["ssl"] = ssl._create_unverified_context()
        self._ws = await websockets.connect(url, **connect_kwargs)
        logger.info(f"RemoteControlClient connected to {url}")

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _request(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self._ws:
            raise RuntimeError("Not connected")
        req_id = uuid.uuid4().hex[:8]
        request = {"method": method, "params": params or {}, "id": req_id}
        if self.token:
            request["token"] = self.token
        await self._ws.send(json.dumps(request))
        response = await self._ws.recv()
        return json.loads(response)

    async def get_status(self) -> Dict[str, Any]:
        result = await self._request("status")
        return result.get("result", result)

    async def list_nodes(self) -> List[Dict[str, Any]]:
        result = await self._request("list_nodes")
        return result.get("result", [])

    async def list_templates(self) -> List[Dict[str, Any]]:
        result = await self._request("list_templates")
        return result.get("result", [])

    async def submit_workflow(self, workflow: Dict[str, Any]) -> str:
        result = await self._request("submit_workflow", {"workflow": workflow})
        return result.get("result", {}).get("task_id", "")

    async def run_template(self, template_id: str) -> str:
        result = await self._request("run_template", {"template_id": template_id})
        return result.get("result", {}).get("task_id", "")

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        result = await self._request("cancel_task", {"task_id": task_id})
        return result.get("result", {})

    async def list_sessions(self) -> List[Dict[str, Any]]:
        result = await self._request("list_sessions")
        return result.get("result", [])

    async def attach_session(self, session_id: str) -> Dict[str, Any]:
        """绑定命名会话 — P2-10。返回会话快照供客户端重放。"""
        result = await self._request("attach_session", {"session_id": session_id})
        return result.get("result", {})
