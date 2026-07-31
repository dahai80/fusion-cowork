"""MCP Server — Fusion-Cowork 作为 MCP 服务端。

重构: 拆分工具注册 (MCPToolRegistry) 与传输层 (StdioTransport)。
MCPServer 作为高层门面，支持 stdio/HTTP 两种传输。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPToolRegistry:
    """MCP 工具注册表 — 管理工具定义与调用。"""

    def __init__(self, permission_manager=None, hook_manager=None):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._node_map: Dict[str, tuple] = {}
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager

    def register_tools(self) -> None:
        """注册所有 Fusion-Cowork 工具。"""
        self._tools = {
            "read_file": {
                "name": "read_file",
                "description": "读取文件内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                    },
                    "required": ["path"],
                },
            },
            "write_file": {
                "name": "write_file",
                "description": "写入文件内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "content": {"type": "string", "description": "文件内容"},
                    },
                    "required": ["path", "content"],
                },
            },
            "list_directory": {
                "name": "list_directory",
                "description": "列出目录内容",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录路径"},
                    },
                    "required": ["path"],
                },
            },
            "run_terminal": {
                "name": "run_terminal",
                "description": "执行终端命令",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"},
                        "timeout": {"type": "integer", "description": "超时秒数"},
                    },
                    "required": ["command"],
                },
            },
            "take_screenshot": {
                "name": "take_screenshot",
                "description": "截取桌面屏幕",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "save_path": {"type": "string", "description": "保存路径"},
                    },
                },
            },
            "clipboard_read": {
                "name": "clipboard_read",
                "description": "读取系统剪贴板",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "clipboard_write": {
                "name": "clipboard_write",
                "description": "写入系统剪贴板",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要写入的文本"},
                    },
                    "required": ["text"],
                },
            },
            "send_notification": {
                "name": "send_notification",
                "description": "发送系统通知",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "通知标题"},
                        "message": {"type": "string", "description": "通知内容"},
                    },
                    "required": ["message"],
                },
            },
            "launch_app": {
                "name": "launch_app",
                "description": "启动 macOS 应用",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string", "description": "应用名称"},
                    },
                    "required": ["app_name"],
                },
            },
            "web_search": {
                "name": "web_search",
                "description": "搜索网页",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                    },
                    "required": ["query"],
                },
            },
            "classify_files": {
                "name": "classify_files",
                "description": "AI 文件分类",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要分类的目录"},
                    },
                },
            },
            "summarize_documents": {
                "name": "summarize_documents",
                "description": "AI 文档摘要",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文档目录"},
                    },
                },
            },
            "desktop_cleanup": {
                "name": "desktop_cleanup",
                "description": "整理桌面文件",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "run_workflow": {
                "name": "run_workflow",
                "description": "执行自动化工作流",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "template": {"type": "string", "description": "模板名称"},
                    },
                    "required": ["template"],
                },
            },
            "skill_list": {
                "name": "skill_list",
                "description": "列出所有可用技能",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "按分类筛选"},
                    },
                },
            },
            "skill_run": {
                "name": "skill_run",
                "description": "执行指定技能",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "技能名称"},
                        "params": {"type": "object", "description": "技能参数"},
                    },
                    "required": ["name"],
                },
            },
        }

        self._node_map = {
            "read_file": ("file_input", lambda a: {"path": a.get("path", "~")}),
            "write_file": ("file_output", lambda a: {"data": {"content": a.get("content", "")}, "output_path": a.get("path", "~/Desktop")}),
            "list_directory": ("file_input", lambda a: {"path": a.get("path", "~"), "recursive": False}),
            "run_terminal": ("shell_exec", lambda a: {"command": a.get("command", ""), "timeout": a.get("timeout", 30)}),
            "take_screenshot": ("screen_capture", lambda a: {"save_path": a.get("save_path", "~/Desktop")}),
            "clipboard_read": ("clipboard", lambda a: {"action": "read"}),
            "clipboard_write": ("clipboard", lambda a: {"text": a.get("text", ""), "action": "write"}),
            "send_notification": ("notification", lambda a: {"title": a.get("title", "Fusion-Cowork"), "message": a.get("message", "")}),
            "launch_app": ("app_lifecycle", lambda a: {"app_name": a.get("app_name", ""), "action": "launch"}),
            "web_search": ("web_search", lambda a: {"query": a.get("query", "")}),
            "classify_files": ("ai_classify", lambda a: {"files": [], "source_path": a.get("path", "~/Desktop")}),
            "summarize_documents": ("ai_summarize", lambda a: {"files": [], "source_path": a.get("path", "~/Desktop")}),
            "desktop_cleanup": ("desktop_clean", lambda a: {"dry_run": False}),
            "run_workflow": ("desktop_clean", lambda a: {"template": a.get("template", "")}),
            "skill_list": ("__skill__", None),
            "skill_run": ("__skill__", None),
        }

        logger.info(f"MCP 工具注册完成: {len(self._tools)} 个")

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回工具列表 (MCP tools/list 格式)。"""
        return list(self._tools.values())

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """调用工具 (MCP tools/call 格式)。"""
        if tool_name not in self._tools:
            return {
                "content": [{"type": "text", "text": f"未知工具: {tool_name}"}],
                "isError": True,
            }

        logger.info(f"MCP 调用: {tool_name}")

        try:
            result = await self._execute_tool(tool_name, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            }
        except Exception as e:
            logger.error(f"MCP 工具执行异常: {e}")
            return {
                "content": [{"type": "text", "text": f"错误: {e}"}],
                "isError": True,
            }

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具: 映射到 NodeRegistry 节点 / SkillRegistry 技能。"""
        if tool_name == "skill_list":
            from ..skills import SkillRegistry, register_builtin_skills
            registry = SkillRegistry()
            register_builtin_skills(registry)
            skills = registry.list_skills()
            return {
                "skills": [
                    {"name": s.name, "description": s.description, "category": s.category}
                    for s in skills
                ],
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

        mapping = self._node_map.get(tool_name)
        if not mapping:
            return {"error": f"未实现: {tool_name}"}

        node_name, param_fn = mapping
        node_params = param_fn(args)

        # Permission check
        if self._permission_manager:
            allowed = await self._permission_manager.check(node_name, "execute", node_params)
            if not allowed:
                logger.warning(f"MCP 工具 '{tool_name}' (node={node_name}) 被权限拒绝")
                if self._hook_manager:
                    from ..engine.hooks import HookEvent
                    await self._hook_manager.fire(HookEvent.PERMISSION_REQUEST, {
                        "tool_name": tool_name, "node_name": node_name,
                        "params": node_params, "allowed": False,
                    })
                return {"error": f"权限拒绝: {node_name}", "status": "denied"}

        # Hook: PRE_NODE_EXECUTE
        if self._hook_manager:
            from ..engine.hooks import HookEvent
            hctx = await self._hook_manager.fire(HookEvent.PRE_NODE_EXECUTE, {
                "tool_name": tool_name, "node_name": node_name,
                "input_data": node_params,
            })
            if hctx and hctx.cancelled:
                return {"error": f"Hook取消: {node_name}", "status": "cancelled"}
            if hctx and hctx.modified_data and "input_data" in hctx.modified_data:
                node_params = hctx.modified_data["input_data"]

        from ..engine.node import NodeRegistry, NodeConfig
        node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
        if not node:
            return {"error": f"节点创建失败: {node_name}"}

        result = await node.execute(node_params)

        # Hook: POST_NODE_EXECUTE
        if self._hook_manager:
            from ..engine.hooks import HookEvent
            await self._hook_manager.fire(HookEvent.POST_NODE_EXECUTE, {
                "tool_name": tool_name, "node_name": node_name,
                "result": result,
            })

        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
            "error": result.error,
        }


class MCPServer:
    """MCP 服务器门面 — 支持 stdio / HTTP 传输。

    用法:
        # stdio 模式 (Claude Code 调用)
        server = MCPServer()
        await server.serve_stdio()

        # HTTP 模式 (远程调用)
        server = MCPServer()
        await server.serve_http(port=9761)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9761,
                 permission_manager=None, hook_manager=None):
        self.host = host
        self.port = port
        self._registry = MCPToolRegistry(
            permission_manager=permission_manager,
            hook_manager=hook_manager,
        )
        self._running = False

    async def serve_stdio(self) -> None:
        """启动 stdio 传输 (供 Claude Code 通过 MCP 协议调用)。"""
        self._registry.register_tools()
        from .mcp_transport import StdioTransport
        transport = StdioTransport(self._registry)
        logger.info("MCP 服务器启动 (stdio 模式)")
        await transport.run()

    async def serve_http(self, event_emitter=None) -> None:
        """启动 HTTP+SSE 传输 (需 [web] 依赖)。"""
        self._registry.register_tools()
        try:
            from .mcp_http import create_http_app
            app = create_http_app(self._registry, event_emitter=event_emitter)
            import uvicorn
            logger.info(f"MCP 服务器启动 (HTTP 模式): {self.host}:{self.port}")
            config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()
        except ImportError:
            logger.error("HTTP 模式需要 [web] 依赖: pip install fusion-cowork[web]")
            raise

    async def start(self) -> None:
        """兼容旧接口 — 默认启动 stdio 模式。"""
        await self.serve_stdio()

    async def stop(self) -> None:
        """停止 MCP 服务器。"""
        self._running = False
        logger.info("MCP 服务器已停止")

    def get_tools_list(self) -> List[Dict[str, Any]]:
        """兼容旧接口。"""
        return self._registry.list_tools()

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """兼容旧接口。"""
        return await self._registry.call_tool(tool_name, arguments)
