"""Fusion-Cowork macOS 系统自动化节点。"""

import subprocess
from typing import Tuple


def run_osascript(script: str, timeout: int = 30) -> Tuple[int, str]:
    """通过 osascript 运行 AppleScript 代码。

    Args:
        script: AppleScript 代码
        timeout: 超时秒数

    Returns:
        (return_code, stdout) 元组
    """
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip()


from .input_nodes import (
    ComputerUseLoopNode,
    KeyboardShortcutNode,
    KeyboardTypeNode,
    MouseClickNode,
    MouseMoveNode,
)
from .system_nodes import (
    AppLifecycleNode,
    ClipboardNode,
    DesktopCleanNode,
    DiskCleanerNode,
    DownloadOrganizerNode,
    FileBatchRenameNode,
    FileClassifierNode,
    FileCopyNode,
    FileDeleteNode,
    FileFindNode,
    FileMoveNode,
    FileWatcherNode,
    NotificationNode,
    OCRNode,
    ScreenCaptureNode,
)

__all__ = [
    "AppLifecycleNode",
    "ClipboardNode",
    "ComputerUseLoopNode",
    "DesktopCleanNode",
    "DiskCleanerNode",
    "DownloadOrganizerNode",
    "FileBatchRenameNode",
    "FileClassifierNode",
    "FileCopyNode",
    "FileDeleteNode",
    "FileFindNode",
    "FileMoveNode",
    "FileWatcherNode",
    "KeyboardShortcutNode",
    "KeyboardTypeNode",
    "MouseClickNode",
    "MouseMoveNode",
    "NotificationNode",
    "OCRNode",
    "ScreenCaptureNode",
]
