"""通用工具节点 — 吸纳自 Squish 内置工具集。"""

from .tool_nodes import ShellExecNode, PythonREPLNode, WebSearchNode, FetchURLNode, ApplyEditNode

__all__ = [
    "ShellExecNode",
    "PythonREPLNode",
    "WebSearchNode",
    "FetchURLNode",
    "ApplyEditNode",
]