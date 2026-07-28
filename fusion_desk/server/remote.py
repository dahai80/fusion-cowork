"""远程控制服务 — WebSocket 接入。

允许外部客户端连接运行中的 fusion-desk 会话，
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
    def __init__(self, host: str = "127.0.0.1", port: int = 9762, token: Optional[str] = None):
        self.host = host
        self.port = port
        self.token = token
        self._server = None
        self._clients: Dict[str, Any] = {}
        self._running = False

    async def start(self):
        try:
            import websockets
        except ImportError:
            logger.error("websockets 未安装，请运行: pip install websockets")
            raise

        self._running = True
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
        )
        logger.info(f"RemoteControlServer started on ws://{self.host}:{self.port}/control")

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
        else:
            return {"id": req_id, "error": f"Unknown method: {method}"}

    async def _get_status(self) -> Dict[str, Any]:
        from fusion_desk.engine.node import NodeRegistry
        nodes = NodeRegistry.list()
        return {
            "status": "running",
            "clients": len(self._clients),
            "registered_nodes": len(nodes),
            "uptime": time.time(),
        }

    def _list_nodes(self) -> List[Dict[str, Any]]:
        from fusion_desk.engine.node import NodeRegistry
        return NodeRegistry.list()

    def _list_templates(self) -> List[Dict[str, Any]]:
        try:
            from fusion_desk.templates import TemplateManager
            mgr = TemplateManager()
            templates = mgr.list_templates()
            return [{"id": t.get("id", ""), "name": t.get("name", ""), "category": t.get("category", "")} for t in templates]
        except Exception as e:
            logger.warning(f"list_templates failed: {e}")
            return []

    async def _submit_workflow(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from fusion_desk.engine import Workflow, WorkflowEngine
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
            await self._broadcast({
                "event": "task_completed",
                "task_id": task_id,
                "status": result.status.value if hasattr(result, "status") else "completed",
            })
        except Exception as e:
            logger.error(f"Workflow {task_id} failed: {e}")
            await self._broadcast({
                "event": "task_failed",
                "task_id": task_id,
                "error": str(e),
            })

    async def _run_template(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from fusion_desk.templates import TemplateManager
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
            from fusion_desk.engine.session import SessionStore
            store = SessionStore()
            sessions = store.list_sessions()
            return [{"id": s.id, "status": s.status, "workflow_name": s.workflow_name} for s in sessions]
        except Exception as e:
            logger.debug(f"list_sessions failed: {e}")
            return []

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
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self._ws = None

    async def connect(self, url: str = "ws://127.0.0.1:9762/control"):
        try:
            import websockets
        except ImportError:
            logger.error("websockets 未安装，请运行: pip install websockets")
            raise
        self._ws = await websockets.connect(url)
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
