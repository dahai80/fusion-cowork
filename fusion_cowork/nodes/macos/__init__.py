"""Fusion-Cowork macOS system automation nodes."""

import subprocess
from typing import Tuple


def run_osascript(script: str, timeout: int = 30) -> Tuple[int, str]:
    """Run AppleScript code through osascript.

    Args:
        script: AppleScript code
        timeout: timeout in seconds

    Returns:
        Tuple of (return_code, stdout)
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
