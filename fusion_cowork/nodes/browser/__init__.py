"""Fusion-Cowork 内嵌浏览器节点 + Chrome CDP 节点。"""

from .browser_nodes import BrowserAutomateNode, BrowserClient, BrowserExtractNode, BrowserManager, BrowserOpenNode
from .cdp_client import CDPClient
from .cdp_nodes import (
    CDPClickNode,
    CDPConsoleNode,
    CDPEmulateNode,
    CDPEvaluateNode,
    CDPFillFormNode,
    CDPFillNode,
    CDPNavigateNode,
    CDPNetworkNode,
    CDPScreenshotNode,
    CDPSnapshotNode,
)

__all__ = [
    "BrowserAutomateNode",
    "BrowserClient",
    "BrowserExtractNode",
    "BrowserManager",
    "BrowserOpenNode",
    "CDPClickNode",
    "CDPClient",
    "CDPConsoleNode",
    "CDPEmulateNode",
    "CDPEvaluateNode",
    "CDPFillFormNode",
    "CDPFillNode",
    "CDPNavigateNode",
    "CDPNetworkNode",
    "CDPScreenshotNode",
    "CDPSnapshotNode",
]
