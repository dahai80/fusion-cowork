"""MCP Server 模式 — Fusion-Desk 作为 MCP 服务端，供 Claude 直接调用。

对标 Claude Cowork 的 MCP 工具协议。
Fusion-Desk 暴露标准 MCP 端点，Claude Desktop/Code 可通过 MCP 协议直接调用
Fusion-Desk 的所有自动化能力。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPServer:
    """MCP 服务器 — 将 Fusion-Desk 能力暴露为 MCP 工具。

    符合 Model Context Protocol 规范，支持：
    - tools/list — 列出所有可用工具
    - tools/call — 调用指定工具
    - 对标 Claude Cowork 的 MCP 协议
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9761):
        self.host = host
        self.port = port
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._server = None
        self._running = False

    def register_tools(self) -> None:
        """注册所有 Fusion-Desk 工具到 MCP 协议。"""
        self._tools = {
            "read_file": {
                "name": "read_file",
                "description": "读取文件内容",
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "save_path": {"type": "string", "description": "保存路径"},
                    },
                },
            },
            "clipboard_read": {
                "name": "clipboard_read",
                "description": "读取系统剪贴板",
                "input_schema": {"type": "object", "properties": {}},
            },
            "clipboard_write": {
                "name": "clipboard_write",
                "description": "写入系统剪贴板",
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
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
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要分类的目录"},
                    },
                },
            },
            "summarize_documents": {
                "name": "summarize_documents",
                "description": "AI 文档摘要",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文档目录"},
                    },
                },
            },
            "desktop_cleanup": {
                "name": "desktop_cleanup",
                "description": "整理桌面文件",
                "input_schema": {"type": "object", "properties": {}},
            },
            "run_workflow": {
                "name": "run_workflow",
                "description": "执行自动化工作流",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "template": {"type": "string", "description": "模板名称"},
                    },
                    "required": ["template"],
                },
            },
        }

    def get_tools_list(self) -> List[Dict[str, Any]]:
        """获取工具列表（MCP tools/list）。"""
        return list(self._tools.values())

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """处理工具调用（MCP tools/call）。"""
        tool = self._tools.get(tool_name)
        if not tool:
            return {"error": f"未知工具: {tool_name}"}

        logger.info(f"MCP 调用: {tool_name}")

        try:
            result = await self._execute_tool(tool_name, arguments)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True}

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用。"""
        # 映射到 Fusion-Desk 节点
        node_map = {
            "read_file": ("file_input", {"path": args.get("path", "~")}),
            "write_file": ("file_output", {"data": {"content": args.get("content", "")}, "output_path": args.get("path", "~/Desktop")}),
            "list_directory": ("file_input", {"path": args.get("path", "~"), "recursive": False}),
            "run_terminal": ("shell_exec", {"command": args.get("command", ""), "timeout": args.get("timeout", 30)}),
            "take_screenshot": ("screen_capture", {"save_path": args.get("save_path", "~/Desktop")}),
            "clipboard_read": ("clipboard", {"action": "read"}),
            "clipboard_write": ("clipboard", {"text": args.get("text", ""), "action": "write"}),
            "send_notification": ("notification", {"title": args.get("title", "Fusion-Desk"), "message": args.get("message", "")}),
            "launch_app": ("app_lifecycle", {"app_name": args.get("app_name", ""), "action": "launch"}),
            "web_search": ("web_search", {"query": args.get("query", "")}),
            "classify_files": ("ai_classify", {"files": [], "source_path": args.get("path", "~/Desktop")}),
            "summarize_documents": ("ai_summarize", {"files": [], "source_path": args.get("path", "~/Desktop")}),
            "desktop_cleanup": ("desktop_clean", {"dry_run": False}),
            "run_workflow": ("desktop_clean", {"template": args.get("template", "")}),
        }

        mapping = node_map.get(tool_name)
        if not mapping:
            return {"error": f"未实现: {tool_name}"}

        node_name, node_params = mapping

        # 创建节点实例并执行
        from ..engine.node import NodeRegistry, NodeConfig
        node = NodeRegistry.create(node_name, config=NodeConfig(params=node_params))
        if not node:
            return {"error": f"节点创建失败: {node_name}"}

        result = await node.execute(node_params)
        return {
            "status": result.status.value,
            "data": result.data,
            "summary": result.summary,
            "error": result.error,
        }

    async def start(self) -> None:
        """启动 MCP 服务器。"""
        self.register_tools()
        self._running = True
        logger.info(f"MCP 服务器启动: {self.host}:{self.port} ({len(self._tools)} 个工具)")

    async def stop(self) -> None:
        """停止 MCP 服务器。"""
        self._running = False
        logger.info("MCP 服务器已停止")