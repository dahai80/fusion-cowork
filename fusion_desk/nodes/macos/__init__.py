"""Fusion-Desk macOS 系统自动化节点。"""

from .system_nodes import (
    DesktopCleanNode,
    DownloadOrganizerNode,
    FileClassifierNode,
    FileBatchRenameNode,
    DiskCleanerNode,
    FileWatcherNode,
    FileCopyNode,
    FileMoveNode,
    FileDeleteNode,
    FileFindNode,
    ScreenCaptureNode,
    ClipboardNode,
    NotificationNode,
    AppLifecycleNode,
    OCRNode,
)
from .input_nodes import (
    MouseMoveNode,
    MouseClickNode,
    KeyboardTypeNode,
    KeyboardShortcutNode,
    ComputerUseLoopNode,
)

__all__ = [
    "DesktopCleanNode",
    "DownloadOrganizerNode",
    "FileClassifierNode",
    "FileBatchRenameNode",
    "DiskCleanerNode",
    "FileWatcherNode",
    "FileCopyNode",
    "FileMoveNode",
    "FileDeleteNode",
    "FileFindNode",
    "ScreenCaptureNode",
    "ClipboardNode",
    "NotificationNode",
    "AppLifecycleNode",
    "OCRNode",
    "MouseMoveNode",
    "MouseClickNode",
    "KeyboardTypeNode",
    "KeyboardShortcutNode",
    "ComputerUseLoopNode",
]