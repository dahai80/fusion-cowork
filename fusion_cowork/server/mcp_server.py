"""MCP Server — Fusion-Cowork as an MCP server.

Refactor: split tool registration (MCPToolRegistry) from the transport layer (StdioTransport).
MCPServer is the high-level facade, supporting both stdio/HTTP transports.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fusion_cowork.observability.trace import get_trace_id

logger = logging.getLogger(__name__)


class MCPToolRegistry:
    """MCP tool registry — manages tool definitions and invocation."""

    def __init__(self, permission_manager=None, hook_manager=None):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._node_map: Dict[str, tuple] = {}
        # E-9: enable a CONFIRM-level PermissionManager by default when none is injected — otherwise run_terminal/
        # file_output/app_lifecycle and other high-risk MCP tools would run without a CONFIRM gate.
        if permission_manager is None:
            from ..engine.permission import PermissionManager

            permission_manager = PermissionManager()
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager

    def register_tools(self) -> None:
        """Register all Fusion-Cowork tools."""
        self._tools = {
            "read_file": {
                "name": "read_file",
                # i18n: user-facing
                "description": "读取文件内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "path": {"type": "string", "description": "文件路径"},
                    },
                    "required": ["path"],
                },
            },
            "write_file": {
                "name": "write_file",
                # i18n: user-facing
                "description": "写入文件内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "path": {"type": "string", "description": "文件路径"},
                        # i18n: user-facing
                        "content": {"type": "string", "description": "文件内容"},
                    },
                    "required": ["path", "content"],
                },
            },
            "list_directory": {
                "name": "list_directory",
                # i18n: user-facing
                "description": "列出目录内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "path": {"type": "string", "description": "目录路径"},
                    },
                    "required": ["path"],
                },
            },
            "run_terminal": {
                "name": "run_terminal",
                # i18n: user-facing
                "description": "执行终端命令",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "command": {"type": "string", "description": "要执行的命令"},
                        # i18n: user-facing
                        "timeout": {"type": "integer", "description": "超时秒数"},
                    },
                    "required": ["command"],
                },
            },
            "take_screenshot": {
                "name": "take_screenshot",
                # i18n: user-facing
                "description": "截取桌面屏幕",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "save_path": {"type": "string", "description": "保存路径"},
                    },
                },
            },
            "clipboard_read": {
                "name": "clipboard_read",
                # i18n: user-facing
                "description": "读取系统剪贴板",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "clipboard_write": {
                "name": "clipboard_write",
                # i18n: user-facing
                "description": "写入系统剪贴板",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "text": {"type": "string", "description": "要写入的文本"},
                    },
                    "required": ["text"],
                },
            },
            "send_notification": {
                "name": "send_notification",
                # i18n: user-facing
                "description": "发送系统通知",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "title": {"type": "string", "description": "通知标题"},
                        # i18n: user-facing
                        "message": {"type": "string", "description": "通知内容"},
                    },
                    "required": ["message"],
                },
            },
            "launch_app": {
                "name": "launch_app",
                # i18n: user-facing
                "description": "启动 macOS 应用",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "app_name": {"type": "string", "description": "应用名称"},
                    },
                    "required": ["app_name"],
                },
            },
            "web_search": {
                "name": "web_search",
                # i18n: user-facing
                "description": "搜索网页",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "query": {"type": "string", "description": "搜索关键词"},
                    },
                    "required": ["query"],
                },
            },
            "classify_files": {
                "name": "classify_files",
                # i18n: user-facing
                "description": "AI 文件分类",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "path": {"type": "string", "description": "要分类的目录"},
                    },
                },
            },
            "summarize_documents": {
                "name": "summarize_documents",
                # i18n: user-facing
                "description": "AI 文档摘要",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "path": {"type": "string", "description": "文档目录"},
                    },
                },
            },
            "desktop_cleanup": {
                "name": "desktop_cleanup",
                # i18n: user-facing
                "description": "整理桌面文件",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "run_workflow": {
                "name": "run_workflow",
                # i18n: user-facing
                "description": "执行自动化工作流",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "template": {"type": "string", "description": "模板名称"},
                    },
                    "required": ["template"],
                },
            },
            "skill_list": {
                "name": "skill_list",
                # i18n: user-facing
                "description": "列出所有可用技能",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "category": {"type": "string", "description": "按分类筛选"},
                    },
                },
            },
            "skill_run": {
                "name": "skill_run",
                # i18n: user-facing
                "description": "执行指定技能",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        # i18n: user-facing
                        "name": {"type": "string", "description": "技能名称"},
                        # i18n: user-facing
                        "params": {"type": "object", "description": "技能参数"},
                    },
                    "required": ["name"],
                },
            },
        }

        self._node_map = {
            "read_file": ("file_input", lambda a: {"path": a.get("path", "~")}),
            "write_file": (
                "file_output",
                lambda a: {"data": {"content": a.get("content", "")}, "output_path": a.get("path", "~/Desktop")},
            ),
            "list_directory": ("file_input", lambda a: {"path": a.get("path", "~"), "recursive": False}),
            "run_terminal": (
                "shell_exec",
                lambda a: {"command": a.get("command", ""), "timeout": a.get("timeout", 30)},
            ),
            "take_screenshot": ("screen_capture", lambda a: {"save_path": a.get("save_path", "~/Desktop")}),
            "clipboard_read": ("clipboard", lambda a: {"action": "read"}),
            "clipboard_write": ("clipboard", lambda a: {"text": a.get("text", ""), "action": "write"}),
            "send_notification": (
                "notification",
                lambda a: {"title": a.get("title", "Fusion-Cowork"), "message": a.get("message", "")},
            ),
            "launch_app": ("app_lifecycle", lambda a: {"app_name": a.get("app_name", ""), "action": "launch"}),
            "web_search": ("web_search", lambda a: {"query": a.get("query", "")}),
            "classify_files": ("ai_classify", lambda a: {"files": [], "source_path": a.get("path", "~/Desktop")}),
            "summarize_documents": ("ai_summarize", lambda a: {"files": [], "source_path": a.get("path", "~/Desktop")}),
            "desktop_cleanup": ("desktop_clean", lambda a: {"dry_run": False}),
            "skill_list": ("__skill__", None),
            "skill_run": ("__skill__", None),
        }

        logger.info(f"MCP tool registration complete: {len(self._tools)} tools")

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the tool list (MCP tools/list format)."""
        return list(self._tools.values())

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool (MCP tools/call format)."""
        if tool_name not in self._tools:
            return {
                # i18n: user-facing
                "content": [{"type": "text", "text": f"未知工具: {tool_name}"}],
                "isError": True,
            }

        logger.info(f"MCP call: {tool_name}")

        # E-9: trace_id carried through the call; on exception only trace_id + a generic message go out, stack logged only (HI-5).
        trace_id = get_trace_id()
        try:
            result = await self._execute_tool(tool_name, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "_trace_id": trace_id,
            }
        except Exception as e:
            logger.error(f"MCP tool execution error trace_id={trace_id} tool={tool_name}: {e}", exc_info=True)
            return {
                "content": [
                    {
                        "type": "text",
                        # i18n: user-facing
                        "text": f"工具执行失败, 请凭 trace_id 排查: {trace_id}",
                    }
                ],
                "isError": True,
                "_trace_id": trace_id,
            }

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool: map to NodeRegistry nodes / SkillRegistry skills."""
        if tool_name == "skill_list":
            from ..skills import SkillRegistry, register_builtin_skills

            registry = SkillRegistry()
            register_builtin_skills(registry)
            skills = registry.list_skills()
            return {
                "skills": [{"name": s.name, "description": s.description, "category": s.category} for s in skills],
                "count": len(skills),
            }

        if tool_name == "skill_run":
            from ..skills import SkillRegistry, register_builtin_skills

            registry = SkillRegistry()
            register_builtin_skills(registry)
            skill_name = args.get("name", "")
            skill_params = args.get("params", {})
            result = await registry.execute(skill_name, **skill_params)
            return {"status": "success", "result": result}

        # E-8: run_workflow goes through TemplateManager + WorkflowEngine; previously mis-mapped to desktop_clean.
        if tool_name == "run_workflow":
            template_id = args.get("template", "")
            if not template_id:
                # i18n: user-facing
                return {"error": "缺少 template 参数"}
            from ..templates import TemplateManager

            mgr = TemplateManager()
            wf = mgr.template_to_workflow(template_id)
            if wf is None:
                logger.warning(f"MCP run_workflow template not found: {template_id}")
                # i18n: user-facing
                return {"error": f"模板不存在: {template_id}", "template": template_id}
            from ..engine import WorkflowEngine

            engine = WorkflowEngine()
            input_data = args.get("input_data", {})
            wf_result = await engine.execute(wf, input_data)
            return {
                "status": wf_result.status.value if hasattr(wf_result, "status") else "completed",
                "data": getattr(wf_result, "output_data", None),
                "summary": getattr(wf_result, "summary", ""),
                "error": getattr(wf_result, "error", None),
                "template": template_id,
            }

        mapping = self._node_map.get(tool_name)
        if not mapping:
            # i18n: user-facing
            return {"error": f"未实现: {tool_name}"}

        node_name, param_fn = mapping
        node_params = param_fn(args)

        # Permission check
        if self._permission_manager:
            allowed = await self._permission_manager.check(node_name, "execute", node_params)
            if not allowed:
                logger.warning(f"MCP tool '{tool_name}' (node={node_name}) denied by permission")
                if self._hook_manager:
                    from ..engine.hooks import HookEvent

                    await self._hook_manager.fire(
                        HookEvent.PERMISSION_REQUEST,
                        {
                            "tool_name": tool_name,
                            "node_name": node_name,
                            "params": node_params,
                            "allowed": False,
                        },
                    )
                # i18n: user-facing
                return {"error": f"权限拒绝: {node_name}", "status": "denied"}

        # Hook: PRE_NODE_EXECUTE
        if self._hook_manager:
            from ..engine.hooks import HookEvent

            hctx = await self._hook_manager.fire(
                HookEvent.PRE_NODE_EXECUTE,
                {
                    "tool_name": tool_name,
                    "node_name": node_name,
                    "input_data": node_params,
                },
            )
            if hctx and hctx.cancelled:
                # i18n: user-facing
                return {"error": f"Hook取消: {node_name}", "status": "cancelled"}
            if hctx and hctx.modified_data and "input_data" in hctx.modified_data:
                node_params = hctx.modified_data["input_data"]

        from ..engine.node import NodeConfig, NodeRegistry

        node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
        if not node:
            # i18n: user-facing
            return {"error": f"节点创建失败: {node_name}"}

        result = await node.execute(node_params)

        # Hook: POST_NODE_EXECUTE
        if self._hook_manager:
            from ..engine.hooks import HookEvent

            await self._hook_manager.fire(
                HookEvent.POST_NODE_EXECUTE,
                {
                    "tool_name": tool_name,
                    "node_name": node_name,
                    "result": result,
                },
            )

        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
            "error": result.error,
        }


class MCPServer:
    """MCP server facade — supports stdio / HTTP transports.

    Usage:
        # stdio mode (invoked by Claude Code)
        server = MCPServer()
        await server.serve_stdio()

        # HTTP mode (remote calls)
        server = MCPServer()
        await server.serve_http(port=11438)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11438, permission_manager=None, hook_manager=None):
        self.host = host
        self.port = port
        self._registry = MCPToolRegistry(
            permission_manager=permission_manager,
            hook_manager=hook_manager,
        )
        self._running = False

    async def serve_stdio(self) -> None:
        """Start the stdio transport (for Claude Code via the MCP protocol)."""
        self._registry.register_tools()
        from .mcp_transport import StdioTransport

        transport = StdioTransport(self._registry)
        logger.info("MCP server started (stdio mode)")
        await transport.run()

    async def serve_http(self, event_emitter=None) -> None:
        """Start the HTTP+SSE transport (requires the [web] extra)."""
        self._registry.register_tools()
        try:
            from .mcp_http import create_http_app

            app = create_http_app(self._registry, event_emitter=event_emitter)
            import uvicorn

            logger.info(f"MCP server started (HTTP mode): {self.host}:{self.port}")
            # MD-1: concurrency capped; the 1MiB request body cap is enforced at the ASGI layer by _BodySizeLimitMiddleware (mcp_http)
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="info",
                limit_concurrency=100,
            )
            server = uvicorn.Server(config)
            await server.serve()
        except ImportError:
            logger.error("HTTP mode requires the [web] extra: pip install fusion-cowork[web]")
            raise

    async def serve_streamable_http(self, event_emitter=None) -> None:
        """Start the Streamable HTTP transport (MCP 2025-03-26, requires the [web] extra)."""
        self._registry.register_tools()
        try:
            from .mcp_http import create_streamable_app

            app = create_streamable_app(self._registry, event_emitter=event_emitter)
            import uvicorn

            logger.info(f"MCP server started (Streamable HTTP mode): {self.host}:{self.port}")
            # MD-1: concurrency capped; the 1MiB request body cap is enforced at the ASGI layer by _BodySizeLimitMiddleware (mcp_http)
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="info",
                limit_concurrency=100,
            )
            server = uvicorn.Server(config)
            await server.serve()
        except ImportError:
            logger.error("Streamable HTTP mode requires the [web] extra: pip install fusion-cowork[web]")
            raise

    async def start(self) -> None:
        """Legacy interface compat — starts stdio mode by default."""
        await self.serve_stdio()

    async def stop(self) -> None:
        """Stop the MCP server."""
        self._running = False
        logger.info("MCP server stopped")

    def get_tools_list(self) -> List[Dict[str, Any]]:
        """Legacy interface compat."""
        return self._registry.list_tools()

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy interface compat."""
        return await self._registry.call_tool(tool_name, arguments)
