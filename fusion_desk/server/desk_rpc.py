"""Desk RPC 服务端 — JSON-RPC 2.0 over Unix Domain Socket。

Fusion-Studio (Swift GUI) 通过 UDS 与 Fusion-Desk 通信。
提供 desk.* 命名空间方法，对标 Studio 端 IPCClient。

协议:
  - 传输: Unix Domain Socket (/tmp/fusion-desk.sock)
  - 编码: JSON-RPC 2.0 (每行一个 JSON 对象)
  - 命名空间: desk.health, desk.nodes.*, desk.workflow.*, desk.agent.*, desk.mlx.*
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SOCK_PATH = "/tmp/fusion-desk.sock"


class DeskRPCServer:
    """Desk RPC 服务端 — 监听 UDS，处理 Studio 发来的 JSON-RPC 请求。"""

    def __init__(self, sock_path: str = DEFAULT_SOCK_PATH,
                 event_emitter=None, session_store=None,
                 permission_manager=None, hook_manager=None):
        self._sock_path = sock_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._handlers: Dict[str, Callable] = {}
        self._event_emitter = event_emitter
        self._session_store = session_store
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._register_handlers()

    def _register_handlers(self) -> None:
        """注册所有 desk.* JSON-RPC 方法。"""
        self._handlers = {
            # 健康检查
            "desk.health": self._handle_health,
            # 节点管理
            "desk.nodes.list": self._handle_nodes_list,
            "desk.nodes.info": self._handle_nodes_info,
            "desk.nodes.execute": self._handle_nodes_execute,
            # 工作流
            "desk.workflow.list": self._handle_workflow_list,
            "desk.workflow.create": self._handle_workflow_create,
            "desk.workflow.run": self._handle_workflow_run,
            "desk.workflow.status": self._handle_workflow_status,
            # 智能体
            "desk.agent.list": self._handle_agent_list,
            "desk.agent.submit": self._handle_agent_submit,
            "desk.agent.status": self._handle_agent_status,
            # MLX
            "desk.mlx.status": self._handle_mlx_status,
            "desk.mlx.start": self._handle_mlx_start,
            "desk.mlx.stop": self._handle_mlx_stop,
            # 系统
            "desk.system.info": self._handle_system_info,
            # 事件订阅
            "desk.events.subscribe": self._handle_events_subscribe,
            "desk.events.recent": self._handle_events_recent,
            # 会话
            "desk.session.list": self._handle_session_list,
            "desk.session.get": self._handle_session_get,
            "desk.session.fork": self._handle_session_fork,
            # 权限
            "desk.permission.check": self._handle_permission_check,
            "desk.permission.approve": self._handle_permission_approve,
            "desk.permission.deny": self._handle_permission_deny,
            "desk.permission.list": self._handle_permission_list,
        }
        logger.info(f"Desk RPC 注册 {len(self._handlers)} 个方法")

    async def start(self) -> None:
        """启动 RPC 服务端。"""
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)

        self._running = True
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._sock_path,
        )
        logger.info(f"Desk RPC 服务启动: {self._sock_path}")

    async def stop(self) -> None:
        """停止 RPC 服务端。"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)
        logger.info("Desk RPC 服务停止")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单个客户端连接。"""
        addr = writer.get_extra_info("peername")
        logger.info(f"Desk RPC 客户端连接: {addr}")

        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"无效 JSON: {e}")
                    await self._write_response(writer, {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    })
                    continue

                response = await self._dispatch(request)
                await self._write_response(writer, response)
        except Exception as e:
            logger.error(f"客户端处理异常: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info(f"Desk RPC 客户端断开: {addr}")

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """分发 JSON-RPC 请求。"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        handler = self._handlers.get(method)
        if not handler:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        try:
            result = await handler(params)
            if req_id is not None:
                return {"jsonrpc": "2.0", "id": req_id, "result": result}
            return {"jsonrpc": "2.0", "result": result}
        except Exception as e:
            logger.error(f"Desk RPC 处理 {method} 异常: {e}")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {e}"},
            }

    async def _write_response(self, writer: asyncio.StreamWriter, response: Dict[str, Any]) -> None:
        """写入 JSON-RPC 响应。"""
        data = json.dumps(response, ensure_ascii=False) + "\n"
        writer.write(data.encode("utf-8"))
        await writer.drain()

    # ── 方法实现 ──

    async def _handle_health(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "service": "fusion-desk", "version": "0.3.0"}

    async def _handle_nodes_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry
        nodes = []
        for name, cls in NodeRegistry._registry.items():
            doc = getattr(cls, "__doc__", "") or ""
            nodes.append({
                "name": name,
                "category": getattr(cls, "category", "unknown"),
                "description": doc.strip()[:100],
            })
        return {"nodes": nodes, "count": len(nodes)}

    async def _handle_nodes_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry
        name = params.get("name", "")
        cls = NodeRegistry._registry.get(name)
        if not cls:
            return {"error": f"节点未注册: {name}"}
        doc = getattr(cls, "__doc__", "") or ""
        return {
            "name": name,
            "category": getattr(cls, "category", "unknown"),
            "description": doc.strip(),
            "params_schema": getattr(cls, "params_schema", {}),
        }

    async def _handle_nodes_execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry, NodeConfig
        name = params.get("name", "")
        node_params = params.get("params", {})
        node = NodeRegistry.create(name, config=NodeConfig(params=node_params))
        if not node:
            return {"error": f"节点创建失败: {name}"}
        result = await node.execute(node_params)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
            "error": result.error,
        }

    async def _handle_workflow_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager
        mgr = TemplateManager()
        templates = mgr.list_templates()
        return {"templates": templates, "count": len(templates)}

    async def _handle_workflow_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..ai import NLWorkflowGenerator
        prompt = params.get("prompt", "")
        if not prompt:
            return {"error": "prompt 不能为空"}
        gen = NLWorkflowGenerator()
        workflow = await gen.generate(prompt)
        return {"workflow": workflow.to_dict() if workflow else None}

    async def _handle_workflow_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine import Workflow, WorkflowEngine
        workflow_def = params.get("workflow", {})
        wf = Workflow.from_dict(workflow_def)
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
        }

    async def _handle_workflow_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "idle", "running_workflows": 0}

    async def _handle_agent_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        agents = [{"id": a.agent_id, "name": a.name, "role": a.role.value} for a in orch._agents.values()]
        return {"agents": agents, "count": len(agents)}

    async def _handle_agent_submit(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..orchestrator import AgentOrchestrator
        task = params.get("task", "")
        if not task:
            return {"error": "task 不能为空"}
        orch = AgentOrchestrator()
        orch.register_default_agents()
        task_id = await orch.submit_task(task)
        return {"task_id": task_id}

    async def _handle_agent_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        task_id = params.get("task_id", "")
        if task_id and task_id in orch._tasks:
            t = orch._tasks[task_id]
            return {"task_id": task_id, "status": t.status.value, "result": t.result}
        return {"status": "idle"}

    async def _handle_mlx_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..ai import FusionMLXClient
        client = FusionMLXClient()
        try:
            status = await client.health_check()
            return {"status": "running", "info": status}
        except Exception:
            return {"status": "stopped"}

    async def _handle_mlx_start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import subprocess
        try:
            result = subprocess.run(
                ["bash", os.path.expanduser("~/claude-home/fusion-mlx/start.sh"), "start"],
                capture_output=True, text=True, timeout=30,
            )
            return {"status": "started", "output": result.stdout[-200:]}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _handle_mlx_stop(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import subprocess
        try:
            result = subprocess.run(
                ["bash", os.path.expanduser("~/claude-home/fusion-mlx/start.sh"), "stop"],
                capture_output=True, text=True, timeout=30,
            )
            return {"status": "stopped", "output": result.stdout[-200:]}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _handle_system_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import platform
        import psutil
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "memory_used_pct": psutil.virtual_memory().percent,
            "disk_free_gb": round(psutil.disk_usage("/").free / (1024**3), 1),
        }

    async def _handle_events_subscribe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        import time as _time
        sub_id, queue = self._event_emitter.subscribe()
        return {"sub_id": sub_id, "message": "已订阅事件流，通过 desk.events.poll 获取"}

    async def _handle_events_recent(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        import time as _time
        since = params.get("since", 0.0)
        events = self._event_emitter.get_buffered(since=since)
        return {
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }

    async def _handle_session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        status = params.get("status")
        limit = params.get("limit", 20)
        sessions = self._session_store.list_sessions(status=status, limit=limit)
        return {
            "count": len(sessions),
            "sessions": [self._session_store.to_dict(s) for s in sessions],
        }

    async def _handle_session_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        s = self._session_store.get(session_id)
        if not s:
            return {"error": f"会话不存在: {session_id}"}
        return self._session_store.to_dict(s)

    async def _handle_session_fork(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        from_step = params.get("from_step", 0)
        forked = self._session_store.fork(session_id, from_step=from_step)
        if not forked:
            return {"error": f"分叉失败: {session_id}"}
        return self._session_store.to_dict(forked)

    async def _handle_permission_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        tool_params = params.get("params", {})
        allowed = self._permission_manager.check(tool_name, tool_params)
        return {"tool_name": tool_name, "allowed": allowed}

    async def _handle_permission_approve(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        scope = params.get("scope", "*")
        self._permission_manager.approve(tool_name, scope=scope)
        return {"tool_name": tool_name, "approved": True, "scope": scope}

    async def _handle_permission_deny(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        scope = params.get("scope", "*")
        self._permission_manager.deny(tool_name, scope=scope)
        return {"tool_name": tool_name, "denied": True, "scope": scope}

    async def _handle_permission_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        return self._permission_manager.to_dict()
