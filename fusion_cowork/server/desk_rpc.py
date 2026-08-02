"""Desk RPC 服务端 — JSON-RPC 2.0 over Unix Domain Socket。

Fusion-Studio (Swift GUI) 通过 UDS 与 Fusion-Cowork 通信。
提供 desk.* 命名空间方法，对标 Studio 端 IPCClient。

协议:
  - 传输: Unix Domain Socket (/tmp/fusion-cowork.sock)
  - 编码: JSON-RPC 2.0 (每行一个 JSON 对象)
  - 命名空间: desk.health, desk.nodes.*, desk.workflow.*, desk.agent.*, desk.mlx.*
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SOCK_PATH = "/tmp/fusion-cowork.sock"


class DeskRPCServer:
    """Desk RPC 服务端 — 监听 UDS，处理 Studio 发来的 JSON-RPC 请求。"""

    def __init__(self, sock_path: str = DEFAULT_SOCK_PATH,
                 event_emitter=None, session_store=None,
                 permission_manager=None, hook_manager=None,
                 space_store=None):
        self._sock_path = sock_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._handlers: Dict[str, Callable] = {}
        self._event_emitter = event_emitter
        self._session_store = session_store
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._space_store = space_store
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
            "desk.nodes.categories": self._handle_nodes_categories,
            # 工作流
            "desk.workflow.list": self._handle_workflow_list,
            "desk.workflow.create": self._handle_workflow_create,
            "desk.workflow.run": self._handle_workflow_run,
            "desk.workflow.status": self._handle_workflow_status,
            "desk.workflow.cancel": self._handle_workflow_cancel,
            # 模板
            "desk.template.list": self._handle_template_list,
            "desk.template.get": self._handle_template_get,
            "desk.template.run": self._handle_template_run,
            # 智能体
            "desk.agent.list": self._handle_agent_list,
            "desk.agent.submit": self._handle_agent_submit,
            "desk.agent.status": self._handle_agent_status,
            "desk.agent.cancel": self._handle_agent_cancel,
            # MLX
            "desk.mlx.status": self._handle_mlx_status,
            "desk.mlx.models": self._handle_mlx_models,
            # 系统
            "desk.system.info": self._handle_system_info,
            # 事件订阅
            "desk.events.subscribe": self._handle_events_subscribe,
            "desk.events.recent": self._handle_events_recent,
            "desk.events.poll": self._handle_events_poll,
            # 会话
            "desk.session.list": self._handle_session_list,
            "desk.session.get": self._handle_session_get,
            "desk.session.fork": self._handle_session_fork,
            "desk.session.create": self._handle_session_create,
            "desk.session.update": self._handle_session_update,
            "desk.session.delete": self._handle_session_delete,
            # 权限
            "desk.permission.check": self._handle_permission_check,
            "desk.permission.approve": self._handle_permission_approve,
            "desk.permission.deny": self._handle_permission_deny,
            "desk.permission.list": self._handle_permission_list,
            "desk.permission.reset": self._handle_permission_reset,
            # 协作空间
            "desk.space.create": self._handle_space_create,
            "desk.space.list": self._handle_space_list,
            "desk.space.get": self._handle_space_get,
            "desk.space.update": self._handle_space_update,
            "desk.space.archive": self._handle_space_archive,
            "desk.space.delete": self._handle_space_delete,
            "desk.space.member.invite": self._handle_space_member_invite,
            "desk.space.member.join": self._handle_space_member_join,
            "desk.space.member.list": self._handle_space_member_list,
            "desk.space.member.remove": self._handle_space_member_remove,
            "desk.space.member.update_role": self._handle_space_member_update_role,
            # 协作空间 - 对话
            "desk.space.chat.send": self._handle_space_chat_send,
            "desk.space.chat.list": self._handle_space_chat_list,
            "desk.space.chat.context": self._handle_space_chat_context,
            # 协作空间 - 知识库
            "desk.space.knowledge.bind": self._handle_space_kb_bind,
            "desk.space.knowledge.status": self._handle_space_kb_status,
            "desk.space.knowledge.upload": self._handle_space_kb_upload,
            "desk.space.knowledge.search": self._handle_space_kb_search,
            "desk.space.knowledge.query": self._handle_space_kb_query,
            "desk.space.knowledge.unbind": self._handle_space_kb_unbind,
            # 协作空间 - Agent
            "desk.space.agent.list": self._handle_space_agent_list,
            "desk.space.agent.add": self._handle_space_agent_add,
            "desk.space.agent.remove": self._handle_space_agent_remove,
            "desk.space.agent.call": self._handle_space_agent_call,
            "desk.space.agent.relay": self._handle_space_agent_relay,
            # 跨产品集成 — fusion-projects
            "desk.project.syncKnowledge": self._handle_project_sync_knowledge,
            "desk.project.importSnapshot": self._handle_project_import_snapshot,
            "desk.project.exportToProject": self._handle_project_export_to_project,
            # 协作空间 - Artifact 权限
            "desk.space.artifact.create": self._handle_space_artifact_create,
            "desk.space.artifact.get": self._handle_space_artifact_get,
            "desk.space.artifact.update": self._handle_space_artifact_update,
            "desk.space.artifact.share": self._handle_space_artifact_share,
            "desk.space.artifact.transfer": self._handle_space_artifact_transfer,
            "desk.space.artifact.list": self._handle_space_artifact_list,
            "desk.space.artifact.delete": self._handle_space_artifact_delete,
            # 侧边栏模块
            "desk.module.register": self._handle_module_register,
            "desk.module.list": self._handle_module_list,
            "desk.module.enable": self._handle_module_enable,
            "desk.module.disable": self._handle_module_disable,
            # 通知推送
            "desk.notification.push": self._handle_notification_push,
            "desk.notification.list": self._handle_notification_list,
            "desk.notification.markRead": self._handle_notification_mark_read,
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
        return {"status": "ok", "service": "fusion-cowork", "version": "0.3.0"}

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
        from ..engine.node import NodeConfig, NodeRegistry
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

    async def _handle_nodes_categories(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine.node import NodeRegistry
        categories: Dict[str, int] = {}
        for name, cls in NodeRegistry._registry.items():
            cat = getattr(cls, "category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
        return {"categories": categories, "count": len(categories)}

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
        template_id = params.get("template_id", "")
        workflow_def = params.get("workflow", {})
        if not workflow_def and template_id:
            from ..templates import TemplateManager
            tmpl = TemplateManager().get_template(template_id)
            if tmpl:
                workflow_def = tmpl
            else:
                return {"error": f"模板不存在: {template_id}"}
        if not workflow_def:
            return {"error": "workflow 或 template_id 必填"}
        wf = Workflow.from_dict(workflow_def)
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        return {
            "id": result.id,
            "status": result.status.value if hasattr(result.status, 'value') else str(result.status),
            "summary": result.result_summary,
            "steps": [s.to_dict() if hasattr(s, 'to_dict') else str(s) for s in result.steps],
            "error": result.error,
        }

    async def _handle_workflow_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "idle", "running_workflows": 0}

    async def _handle_workflow_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine import WorkflowEngine
        execution_id = params.get("execution_id", "")
        if not execution_id:
            return {"error": "execution_id 不能为空"}
        engine = WorkflowEngine()
        engine.cancel(execution_id)
        return {"status": "cancelled", "execution_id": execution_id}

    async def _handle_template_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager
        mgr = TemplateManager()
        category = params.get("category", "")
        templates = mgr.list_templates()
        if category:
            templates = [t for t in templates if t.get("category") == category]
        return {"templates": templates, "count": len(templates)}

    async def _handle_template_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..templates import TemplateManager
        template_id = params.get("template_id", "") or params.get("id", "")
        mgr = TemplateManager()
        template = mgr.get_template(template_id)
        if not template:
            return {"error": f"模板不存在: {template_id}"}
        return template

    async def _handle_template_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..engine import Workflow, WorkflowEngine
        from ..templates import TemplateManager
        template_id = params.get("template_id", "")
        _variables = params.get("variables", {})
        mgr = TemplateManager()
        template = mgr.get_template(template_id)
        if not template:
            return {"error": f"模板不存在: {template_id}"}
        wf_data = template.get("workflow", template)
        wf = Workflow.from_dict(wf_data)
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
        }

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
        t = orch.get_task(task_id)
        if t:
            return {"task_id": task_id, "status": t.status.value, "result": t.result}
        return {"status": "idle"}

    async def _handle_agent_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..orchestrator import AgentOrchestrator
        task_id = params.get("task_id", "")
        if not task_id:
            return {"error": "task_id 不能为空"}
        orch = AgentOrchestrator()
        orch.cancel_task(task_id)
        return {"status": "cancelled", "task_id": task_id}

    async def _handle_mlx_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..ai import FusionMLXClient
        client = FusionMLXClient()
        try:
            status = await client.health_check()
            return {"status": "running", "info": status}
        except Exception:
            return {"status": "stopped"}

    async def _handle_mlx_models(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..ai import FusionMLXClient
        client = FusionMLXClient()
        try:
            models = await client.list_models()
            return {"models": models}
        except Exception as e:
            return {"error": str(e), "models": []}

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
        sub_id, queue = self._event_emitter.subscribe()
        return {"sub_id": sub_id, "message": "已订阅事件流，通过 desk.events.poll 获取"}

    async def _handle_events_recent(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        since = params.get("since", 0.0)
        events = self._event_emitter.get_buffered(since=since)
        return {
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }

    async def _handle_events_poll(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._event_emitter:
            return {"error": "EventEmitter 未配置"}
        sub_id = params.get("sub_id", "")
        events = []
        if sub_id and sub_id in self._event_emitter._subscribers:
            queue = self._event_emitter._subscribers[sub_id]
            while not queue.empty():
                events.append(queue.get_nowait().to_dict())
        return {"count": len(events), "events": events}

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

    async def _handle_session_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        from fusion_cowork.engine.session import Session
        name = params.get("name", "")
        workflow_id = params.get("workflow_id", "")
        workflow_name = name or params.get("workflow_name", "")
        initial_input = params.get("initial_input", {})
        metadata = params.get("metadata", {})
        if description := params.get("description", ""):
            metadata["description"] = description
        if space_id := params.get("space_id", ""):
            metadata["space_id"] = space_id
        if user_id := params.get("user_id", ""):
            metadata["user_id"] = user_id
        session = Session(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            initial_input=initial_input,
            metadata=metadata,
        )
        self._session_store.save(session)
        logger.info(f"Session created: {session.id} workflow={workflow_name}")
        return self._session_store.to_dict(session)

    async def _handle_session_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        session = self._session_store.get(session_id)
        if not session:
            return {"error": f"会话不存在: {session_id}"}
        updates = params.get("updates", {})
        if "name" in updates or "workflow_name" in updates:
            session.workflow_name = updates.get("workflow_name", updates.get("name", session.workflow_name))
        if "status" in updates:
            self._session_store.update_status(session_id, updates["status"])
        if "steps" in updates:
            self._session_store.update_steps(session_id, updates["steps"])
        if "metadata" in updates:
            session.metadata.update(updates["metadata"])
            self._session_store.save(session)
        session = self._session_store.get(session_id)
        return self._session_store.to_dict(session)

    async def _handle_session_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_store:
            return {"error": "SessionStore 未配置"}
        session_id = params.get("session_id", "")
        deleted = self._session_store.delete(session_id)
        return {"deleted": deleted, "session_id": session_id}

    async def _handle_permission_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        tool_name = params.get("tool_name", "")
        tool_params = params.get("params", {})
        allowed = await self._permission_manager.check(tool_name, "execute", tool_params)
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

    async def _handle_permission_reset(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._permission_manager:
            return {"error": "PermissionManager 未配置"}
        self._permission_manager.reset()
        return {"status": "reset"}

    # ── 协作空间 ──

    async def _handle_space_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        name = params.get("name", "")
        owner_id = params.get("owner_id", "local_user")
        description = params.get("description", "")
        collab_mode = params.get("collab_mode", "local")
        try:
            sp = await svc.create(name=name, owner_id=owner_id,
                                  description=description, collab_mode=collab_mode)
            return sp.to_dict()
        except Exception as e:
            logger.error(f"space.create 失败: {e}")
            return {"error": str(e)}

    async def _handle_space_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        status = params.get("status")
        owner_id = params.get("owner_id")
        limit = params.get("limit", 20)
        spaces = await svc.list(status=status, owner_id=owner_id, limit=limit)
        return {"spaces": [s.to_dict() for s in spaces], "count": len(spaces)}

    async def _handle_space_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        sp = await svc.get(space_id)
        if not sp:
            return {"error": f"空间不存在: {space_id}"}
        return sp.to_dict()

    async def _handle_space_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        updates = params.get("updates", {})
        sp = await svc.update(space_id, **updates)
        if not sp:
            return {"error": f"空间不存在: {space_id}"}
        return sp.to_dict()

    async def _handle_space_archive(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        try:
            result = await svc.archive(space_id)
            return {"space_id": space_id, "archived": bool(result)}
        except Exception as e:
            logger.error(f"space.archive 失败: {e}")
            return {"error": str(e)}

    async def _handle_space_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceService
        svc = SpaceService(self._space_store)
        space_id = params.get("space_id", "")
        await svc.delete(space_id)
        return {"space_id": space_id, "deleted": True}

    async def _handle_space_member_invite(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission
        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        inviter_id = params.get("inviter_id", "")
        if not inviter_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            inviter_id = sp.owner_id if sp else "local_user"
        role = params.get("role", "member")
        max_uses = params.get("max_uses", 0)
        expires_hours = params.get("expires_hours", 0)
        try:
            code = await svc.invite(space_id, inviter_id, role=role,
                                    max_uses=max_uses, expires_hours=expires_hours)
            return {"invite_code": code, "space_id": space_id}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_member_join(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission
        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        invite_code = params.get("invite_code", "")
        user_id = params.get("user_id", "")
        display_name = params.get("display_name", "")
        try:
            member = await svc.join(invite_code, user_id=user_id, display_name=display_name)
            return member.to_dict()
        except ValueError as e:
            return {"error": str(e)}

    async def _handle_space_member_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        members = await self._space_store.list_members(space_id)
        return {"members": [m.to_dict() for m in members], "count": len(members)}

    async def _handle_space_member_remove(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission
        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            removed = await svc.remove(space_id, user_id, operator_id=operator_id)
            return {"space_id": space_id, "user_id": user_id, "removed": removed}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_member_update_role(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceMemberService, SpacePermission
        perm = SpacePermission(self._space_store)
        svc = SpaceMemberService(self._space_store, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        new_role = params.get("new_role", "member")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            member = await svc.update_role(space_id, user_id, new_role, operator_id=operator_id)
            return member.to_dict()
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_chat_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceChatService, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        content = params.get("content", "")
        agent_id = params.get("agent_id")
        try:
            msg = await chat_svc.send_message(space_id, user_id, content, agent_id=agent_id)
            return msg.to_dict()
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_chat_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceChatService, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        limit = params.get("limit", 50)
        offset = params.get("offset", 0)
        try:
            msgs = await chat_svc.list_messages(space_id, limit=limit, offset=offset)
            return {"messages": [m.to_dict() for m in msgs]}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_chat_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceChatService, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        limit = params.get("limit", 100)
        try:
            msgs = await chat_svc.get_context(space_id, limit=limit)
            return {"messages": [m.to_dict() for m in msgs]}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_kb_bind(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        kb_id = params.get("kb_id")
        try:
            result = await kb_svc.bind_kb(space_id, operator_id, kb_id=kb_id)
            return {"kb_id": result}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_kb_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        try:
            return await kb_svc.get_kb_status(space_id)
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_kb_upload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import KBClient
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        file_path = params.get("file_path", "")
        try:
            result = await kb_svc.upload_document(space_id, operator_id, file_path)
            return {"result": result}
        except (PermissionError, FileNotFoundError) as e:
            return {"error": str(e)}

    async def _handle_space_kb_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import KBClient
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        try:
            results = await kb_svc.search(space_id, query, top_k=top_k)
            return {"results": results}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_kb_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import KBClient
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        space_id = params.get("space_id", "")
        question = params.get("question", "")
        top_k = params.get("top_k", 5)
        try:
            answer = await kb_svc.query(space_id, question, top_k=top_k)
            return {"answer": answer}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_kb_unbind(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.space import SpaceKBService, SpacePermission
        perm = SpacePermission(self._space_store)
        kb_svc = SpaceKBService(self._space_store, None, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            await kb_svc.unbind_kb(space_id, operator_id)
            return {"status": "unbound"}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        try:
            agents = await rt.list_agents(space_id)
            return {"agents": agents, "count": len(agents)}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_space_agent_add(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        name = params.get("name", "") or params.get("agent_name", "")
        agent_type = params.get("agent_type", "assistant")
        system_prompt = params.get("system_prompt", "")
        enable_rag = params.get("enable_rag", False)
        config = params.get("config", {})
        try:
            result = await rt.add_agent(
                space_id=space_id, operator_id=operator_id, name=name,
                agent_type=agent_type, system_prompt=system_prompt,
                enable_rag=enable_rag, config=config,
            )
            return result
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_remove(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        operator_id = params.get("operator_id", "")
        if not operator_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            operator_id = sp.owner_id if sp else "local_user"
        try:
            removed = await rt.remove_agent(space_id, agent_id, operator_id)
            return {"agent_id": agent_id, "removed": removed}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_agent_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceAgentRuntime, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        rt = SpaceAgentRuntime(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        message = params.get("message", "")
        model = params.get("model", "")
        try:
            reply = await rt.call_agent(space_id, agent_id, user_id, message, model=model)
            return {"content": reply}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_agent_relay(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space import SpaceChatService, SpacePermission
        perm = SpacePermission(self._space_store)
        mlx = FusionMLXClient()
        chat_svc = SpaceChatService(self._space_store, mlx, perm)
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        agent_ids = params.get("agent_ids", [])
        message = params.get("message", "")
        model = params.get("model", "")
        try:
            results = await chat_svc.relay_agents(
                space_id, user_id, agent_ids, message, model=model,
            )
            return {"results": results}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    # ── 跨产品集成：fusion-projects ──

    async def _handle_project_sync_knowledge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收外部项目知识库文件同步到协同空间。

        params:
            space_id: 目标空间 ID
            files: [{"name": "x.pdf", "content": "<base64>", "folder": "需求文档"}, ...]
            operator_id: 操作者 (default: "project_sync")
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        from fusion_cowork.ai.mlx_client import KBClient
        from fusion_cowork.space import SpaceKBService, SpacePermission
        space_id = params.get("space_id", "")
        files = params.get("files", [])
        operator_id = params.get("operator_id", "project_sync")
        if not space_id:
            return {"error": "space_id 必填"}
        if not files:
            return {"error": "files 不能为空"}
        perm = SpacePermission(self._space_store)
        kb_client = KBClient()
        kb_svc = SpaceKBService(self._space_store, kb_client, perm)
        synced = []
        errors = []
        for f in files:
            name = f.get("name", "")
            content_b64 = f.get("content", "")
            folder = f.get("folder", "")
            if not name or not content_b64:
                errors.append({"name": name, "error": "name/content 缺失"})
                continue
            try:
                import base64
                content_bytes = base64.b64decode(content_b64)
                result = await kb_svc.upload_document(
                    space_id, operator_id, name, content_bytes, folder=folder,
                )
                synced.append({"name": name, "result": result})
            except Exception as e:
                logger.error(f"syncKnowledge: {name} failed: {e}")
                errors.append({"name": name, "error": str(e)})
        logger.info(f"desk.project.syncKnowledge space={space_id} synced={len(synced)} errors={len(errors)}")
        return {"synced": synced, "errors": errors}

    async def _handle_project_import_snapshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收会话快照导入到协同空间。

        params:
            space_id: 目标空间 ID
            snapshot: {"title": "...", "messages": [...], "instructionSnapshot": "...", "agentId": "..."}
            operator_id: 操作者 (default: "project_import")
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        import uuid

        from fusion_cowork.space.models import SpaceMessage
        space_id = params.get("space_id", "")
        snapshot = params.get("snapshot", {})
        operator_id = params.get("operator_id", "project_import")
        if not space_id:
            return {"error": "space_id 必填"}
        if not snapshot:
            return {"error": "snapshot 不能为空"}
        space = await self._space_store.get_space(space_id)
        if not space:
            return {"error": f"空间 {space_id} 不存在"}
        messages_data = snapshot.get("messages", [])
        imported = 0
        errors = []
        for msg in messages_data:
            try:
                m = SpaceMessage(
                    id=msg.get("id", f"msg_{uuid.uuid4().hex[:8]}"),
                    space_id=space_id,
                    user_id=msg.get("user_id", operator_id),
                    content=msg.get("content", ""),
                    content_type=msg.get("content_type", "text"),
                    agent_id=msg.get("agent_id", snapshot.get("agentId", "")),
                    parent_msg_id=msg.get("parent_msg_id"),
                    created_at=msg.get("created_at", ""),
                )
                await self._space_store.add_message(m)
                imported += 1
            except Exception as e:
                logger.error(f"importSnapshot: message failed: {e}")
                errors.append({"id": msg.get("id", "?"), "error": str(e)})
        logger.info(f"desk.project.importSnapshot space={space_id} imported={imported} errors={len(errors)}")
        return {"imported": imported, "errors": errors}

    async def _handle_project_export_to_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """导出协同空间内容到 fusion-projects。

        params:
            space_id: 源空间 ID
            items: {"files": true, "chatHistory": true, "artifacts": false}
            target_project_id: 目标项目 ID (记录用)
        """
        if not self._space_store:
            return {"error": "SpaceStore 未配置"}
        space_id = params.get("space_id", "")
        items = params.get("items", {})
        target_project_id = params.get("target_project_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        space = await self._space_store.get_space(space_id)
        if not space:
            return {"error": f"空间 {space_id} 不存在"}
        export_data: Dict[str, Any] = {"space_id": space_id, "space_name": space.name}
        if items.get("chatHistory", True):
            messages = await self._space_store.get_messages(space_id, limit=1000)
            export_data["messages"] = [
                {"id": m.id, "user_id": m.user_id, "content": m.content,
                 "content_type": m.content_type, "agent_id": m.agent_id or "",
                 "created_at": m.created_at}
                for m in messages
            ]
        if items.get("files", True):
            agents = await self._space_store.list_agents(space_id)
            export_data["agents"] = agents
        export_data["target_project_id"] = target_project_id
        logger.info(f"desk.project.exportToProject space={space_id} target={target_project_id}")
        return {"export_data": export_data}

    # ── Artifact 权限 Handlers ──

    def _get_artifact_svc(self):
        from ..space.artifact import SpaceArtifactService
        from ..space.permission import SpacePermission
        if not self._space_store:
            return None, "space_store 未初始化"
        perm = SpacePermission(self._space_store)
        return SpaceArtifactService(self._space_store, perm), None

    async def _handle_space_artifact_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "") or params.get("owner_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.create_artifact(
                space_id=space_id,
                owner_user_id=user_id,
                name=params.get("name", ""),
                artifact_type=params.get("artifact_type", "document"),
                content=params.get("content", ""),
                metadata=params.get("metadata"),
            )
            return {"artifact": result}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_artifact_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            art = await svc.get_artifact(space_id, artifact_id, user_id)
            if not art:
                return {"error": f"Artifact {artifact_id} 不存在"}
            return {"artifact": art}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_artifact_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.update_artifact(
                space_id=space_id, artifact_id=artifact_id, user_id=user_id,
                content=params.get("content", ""),
                name=params.get("name", ""),
                metadata=params.get("metadata"),
            )
            return {"artifact": result}
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_share(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            result = await svc.share_artifact(space_id, artifact_id, user_id)
            return result
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_transfer(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        from_user = params.get("from_user_id", "")
        to_user = params.get("to_user_id", "")
        if not all([space_id, artifact_id, from_user, to_user]):
            return {"error": "space_id, artifact_id, from_user_id, to_user_id 必填"}
        try:
            result = await svc.transfer_ownership(space_id, artifact_id, from_user, to_user)
            return result
        except (PermissionError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_space_artifact_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        user_id = params.get("user_id", "")
        if not space_id:
            return {"error": "space_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        try:
            artifacts = await svc.list_artifacts(
                space_id, user_id, params.get("artifact_type", ""),
            )
            return {"artifacts": artifacts}
        except PermissionError as e:
            return {"error": str(e)}

    async def _handle_space_artifact_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_artifact_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "")
        artifact_id = params.get("artifact_id", "")
        user_id = params.get("user_id", "")
        if not all([space_id, artifact_id]):
            return {"error": "space_id, artifact_id 必填"}
        if not user_id:
            sp = await self._space_store.get_space(space_id) if self._space_store else None
            user_id = sp.owner_id if sp else "local_user"
        removed = await svc.delete_artifact(space_id, artifact_id, user_id)
        return {"deleted": removed}

    # ── 侧边栏模块 Handlers ──

    def _get_module_registry(self):
        from ..space.fsb import ModuleRegistry
        if not self._space_store:
            return None, "space_store 未初始化"
        return ModuleRegistry(self._space_store), None

    async def _handle_module_register(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        name = params.get("name", "")
        if not module_id or not name:
            return {"error": "id 和 name 必填"}
        result = await reg.register_module(
            module_id=module_id, name=name,
            icon=params.get("icon", ""),
            route_path=params.get("route_path", ""),
            enabled=params.get("enabled", True),
            metadata=params.get("metadata"),
        )
        return {"module": result}

    async def _handle_module_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        modules = await reg.list_modules(enabled_only=params.get("enabled_only", False))
        return {"modules": modules}

    async def _handle_module_enable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        if not module_id:
            return {"error": "id 必填"}
        await reg.enable_module(module_id)
        return {"enabled": True}

    async def _handle_module_disable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        reg, err = self._get_module_registry()
        if err:
            return {"error": err}
        module_id = params.get("id", "")
        if not module_id:
            return {"error": "id 必填"}
        await reg.disable_module(module_id)
        return {"disabled": True}

    # ── 通知推送 Handlers ──

    def _get_notification_svc(self):
        from ..space.fsb import NotificationService
        if not self._space_store:
            return None, "space_store 未初始化"
        return NotificationService(self._space_store), None

    async def _handle_notification_push(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        space_id = params.get("space_id", "") or ""
        user_id = params.get("user_id", "") or "local_user"
        title = params.get("title", "")
        if not title:
            return {"error": "title 必填"}
        result = await svc.push_notification(
            space_id=space_id, user_id=user_id,
            notification_type=params.get("type", "approval"),
            title=title,
            content=params.get("content", ""),
            metadata=params.get("metadata"),
        )
        return {"notification": result}

    async def _handle_notification_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        user_id = params.get("user_id", "") or "local_user"
        notifications = await svc.list_notifications(
            user_id, unread_only=params.get("unread_only", False),
        )
        return {"notifications": notifications}

    async def _handle_notification_mark_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        svc, err = self._get_notification_svc()
        if err:
            return {"error": err}
        notif_id = params.get("id", "") or params.get("notification_id", "")
        if not notif_id:
            return {"error": "id 或 notification_id 必填"}
        await svc.mark_read(notif_id)
        return {"read": True}
