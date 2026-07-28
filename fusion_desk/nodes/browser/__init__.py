"""Fusion-Desk 内嵌浏览器节点 + Chrome CDP 节点。"""

from .browser_nodes import BrowserOpenNode, BrowserExtractNode, BrowserAutomateNode, BrowserClient, BrowserManager
from .cdp_client import CDPClient
from .cdp_nodes import (
    CDPNavigateNode, CDPSnapshotNode, CDPClickNode, CDPFillNode,
    CDPFillFormNode, CDPScreenshotNode, CDPEvaluateNode,
    CDPEmulateNode, CDPNetworkNode, CDPConsoleNode,
)

__all__ = [
    "BrowserOpenNode",
    "BrowserExtractNode",
    "BrowserAutomateNode",
    "BrowserClient",
    "BrowserManager",
    "CDPClient",
    "CDPNavigateNode",
    "CDPSnapshotNode",
    "CDPClickNode",
    "CDPFillNode",
    "CDPFillFormNode",
    "CDPScreenshotNode",
    "CDPEvaluateNode",
    "CDPEmulateNode",
    "CDPNetworkNode",
    "CDPConsoleNode",
]
