"""通用工具节点 — 吸纳自 Squish 内置工具集。"""

from .tool_nodes import ApplyEditNode, FetchURLNode, PythonREPLNode, ShellExecNode, WebSearchNode

__all__ = [
    "ApplyEditNode",
    "FetchURLNode",
    "PythonREPLNode",
    "ShellExecNode",
    "WebSearchNode",
]
