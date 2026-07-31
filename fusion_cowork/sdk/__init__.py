"""Fusion-Cowork SDK — async HTTP client + local fallback."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List

logger = logging.getLogger(__name__)


class FusionCoworkSDK:
    """SDK entry — programmatic access to fusion-cowork."""

    def __init__(self, base_url: str = "http://localhost:9761"):
        self._base_url = base_url.rstrip("/")
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx
                self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
            except ImportError:
                logger.warning("httpx not installed, SDK using local mode")
                self._client = "local"
        return self._client

    async def _request(self, method: str, path: str, json_data: dict = None) -> Any:
        client = await self._get_client()
        if client == "local":
            return await self._local_request(method, path, json_data)
        try:
            resp = await client.request(method, path, json=json_data)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"SDK HTTP failed: {method} {path} -> {e}, falling back to local")
            return await self._local_request(method, path, json_data)

    async def _local_request(self, method: str, path: str, json_data: dict = None) -> Any:
        if path.startswith("/api/nodes") and method == "GET":
            return await self._local_list_nodes()
        if path.startswith("/api/templates") and method == "GET":
            return await self._local_list_templates()
        if path.startswith("/api/workflows") and method == "POST":
            return await self._local_run_workflow(json_data)
        if path.startswith("/api/nodes/") and method == "POST":
            node_name = path.split("/")[-1].replace("/exec", "")
            return await self._local_exec_node(node_name, json_data)
        return {"error": f"local mode unsupported: {method} {path}"}

    _node_modules_loaded = False

    @classmethod
    def _ensure_node_modules(cls) -> None:
        if cls._node_modules_loaded:
            return
        import fusion_cowork.nodes.macos  # noqa: F401 — trigger registration
        import fusion_cowork.nodes.ai  # noqa: F401
        import fusion_cowork.nodes.io  # noqa: F401
        import fusion_cowork.nodes.logic  # noqa: F401
        import fusion_cowork.nodes.tools  # noqa: F401
        import fusion_cowork.nodes.browser  # noqa: F401
        cls._node_modules_loaded = True

    async def _local_list_nodes(self) -> List[Dict[str, Any]]:
        from fusion_cowork.engine.node import NodeRegistry
        self._ensure_node_modules()
        return NodeRegistry.list()

    async def _local_list_templates(self) -> List[Dict[str, Any]]:
        from fusion_cowork.templates import TemplateManager
        mgr = TemplateManager()
        return [{"id": t.get("id", ""), "name": t.get("name", "")} for t in mgr.list_templates()]

    async def _local_run_workflow(self, data: dict) -> Dict[str, Any]:
        from fusion_cowork.engine import Workflow, WorkflowEngine
        self._ensure_node_modules()
        wf_def = data.get("workflow", {})
        if not wf_def:
            return {"error": "missing workflow param"}
        wf = Workflow.from_dict(wf_def)
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        return {
            "status": result.status.value,
            "steps": len(result.steps),
            "total_time": result.total_time,
        }

    async def _local_exec_node(self, node_name: str, data: dict) -> Dict[str, Any]:
        from fusion_cowork.engine.node import NodeRegistry, NodeConfig
        self._ensure_node_modules()
        params = data.get("params", {})
        node = NodeRegistry.create(node_name, config=NodeConfig(params=params))
        if not node:
            return {"error": f"node not found: {node_name}"}
        result = await node.execute(params)
        return {"status": result.status.value, "data": result.data}

    # ── workflow ──

    async def create_workflow(self, name: str, nodes: list, edges: list) -> str:
        data = {"name": name, "nodes": nodes, "edges": edges}
        result = await self._request("POST", "/api/workflows", data)
        return result.get("workflow_id", "")

    async def run_workflow(self, workflow_id: str, inputs: dict = None) -> dict:
        data = {"workflow_id": workflow_id, "inputs": inputs or {}}
        return await self._request("POST", f"/api/workflows/{workflow_id}/run", data)

    async def get_workflow_status(self, workflow_id: str) -> dict:
        return await self._request("GET", f"/api/workflows/{workflow_id}")

    # ── template ──

    async def list_templates(self, category: str = "") -> list:
        path = "/api/templates"
        if category:
            path += f"?category={category}"
        result = await self._request("GET", path)
        return result if isinstance(result, list) else result.get("templates", [])

    async def run_template(self, template_id: str, params: dict = None) -> dict:
        data = {"template_id": template_id, "params": params or {}}
        return await self._request("POST", f"/api/templates/{template_id}/run", data)

    # ── node ──

    async def list_nodes(self, category: str = "") -> list:
        path = "/api/nodes"
        if category:
            path += f"?category={category}"
        result = await self._request("GET", path)
        return result if isinstance(result, list) else result.get("nodes", [])

    async def execute_node(self, node_name: str, params: dict) -> dict:
        return await self._request("POST", f"/api/nodes/{node_name}/exec", {"params": params})

    # ── agent ──

    async def submit_task(self, description: str, input_data: dict = None) -> str:
        data = {"description": description, "input_data": input_data or {}}
        result = await self._request("POST", "/api/tasks", data)
        return result.get("task_id", "")

    async def get_task_status(self, task_id: str) -> dict:
        return await self._request("GET", f"/api/tasks/{task_id}")

    # ── stream ──

    async def stream_workflow(self, workflow_id: str) -> AsyncIterator[dict]:
        client = await self._get_client()
        if client == "local":
            yield {"error": "local mode does not support SSE stream"}
            return
        try:
            async with client.stream("GET", f"/api/stream/{workflow_id}") as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])
        except Exception as e:
            yield {"error": str(e)}

    async def close(self) -> None:
        if self._client and self._client != "local":
            await self._client.aclose()
            self._client = None
