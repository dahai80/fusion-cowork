"""Capability comparison matrix — defines capability dimensions and scoring for Claude Cowork vs Fusion-Cowork."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CapabilityLevel(Enum):
    NONE = 0
    PARTIAL = 1
    FULL = 2
    ADVANCED = 3


@dataclass
class Capability:
    id: str
    name: str
    category: str
    description: str = ""
    cowork_level: CapabilityLevel = CapabilityLevel.NONE
    desk_level: CapabilityLevel = CapabilityLevel.NONE
    cowork_detail: str = ""
    desk_detail: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def parity(self) -> float:
        if self.cowork_level.value == 0:
            return 1.0 if self.desk_level.value > 0 else 0.0
        return min(self.desk_level.value / self.cowork_level.value, 1.5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "cowork_level": self.cowork_level.name,
            "desk_level": self.desk_level.name,
            "cowork_detail": self.cowork_detail,
            "desk_detail": self.desk_detail,
            "parity": round(self.parity, 2),
            "tags": self.tags,
        }


class CapabilityMatrix:
    """Capability comparison matrix — manages the Capability list and computes parity rates."""

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}
        self._init_default()

    def _init_default(self) -> None:
        defaults = [
            # ── Desktop automation ──
            Capability(
                "file_ops",
                "File operations",
                "Desktop automation",
                "Read/write/copy/move/delete/batch rename/classify",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="Via terminal commands",
                desk_detail="Native nodes file_input/output/copy/move/delete/batch_rename/classifier/find",
            ),
            Capability(
                "desktop_clean",
                "Desktop tidy",
                "Desktop automation",
                "Auto-tidy desktop files by type/rule",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="Terminal command orchestration",
                desk_detail="DesktopClean node + AI classification",
            ),
            Capability(
                "download_organize",
                "Downloads tidy",
                "Desktop automation",
                "Auto-tidy the downloads folder by type/date",
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="Requires manual command orchestration",
                desk_detail="DownloadOrganizer node",
            ),
            Capability(
                "disk_clean",
                "Disk cleanup",
                "Desktop automation",
                "Clean caches/temp files/large files",
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Requires hand-written scripts",
                desk_detail="DiskCleaner node",
            ),
            # ── macOS integration ──
            Capability(
                "screen_capture",
                "Screen capture",
                "macOS integration",
                "Desktop screenshot + AI analysis",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="screencapture + AI",
                desk_detail="ScreenCapture node",
            ),
            Capability(
                "clipboard",
                "Clipboard",
                "macOS integration",
                "Read/write the system clipboard",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="pbcopy/pbpaste",
                desk_detail="Clipboard node",
            ),
            Capability(
                "notification",
                "Notification",
                "macOS integration",
                "Send system notifications",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="osascript notification",
                desk_detail="Notification node",
            ),
            Capability(
                "app_lifecycle",
                "App management",
                "macOS integration",
                "Launch/quit/activate/list apps",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="open/killall/osascript",
                desk_detail="AppLifecycle node",
            ),
            Capability(
                "ocr",
                "OCR text recognition",
                "macOS integration",
                "Image/screenshot text recognition",
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Requires tesseract installed",
                desk_detail="Vision + MLX OCR node",
            ),
            # ── AI capabilities ──
            Capability(
                "ai_classify",
                "AI classification",
                "AI capabilities",
                "Content-based intelligent file classification",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude classifies directly",
                desk_detail="AIClassify node + fusion-mlx",
            ),
            Capability(
                "ai_summarize",
                "AI summary",
                "AI capabilities",
                "Intelligent document content summarization",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude summarizes directly",
                desk_detail="AISummarize node + fusion-mlx",
            ),
            Capability(
                "ai_rename",
                "AI naming",
                "AI capabilities",
                "Content-based intelligent file naming",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude names directly",
                desk_detail="AIGenerateName node",
            ),
            Capability(
                "local_llm",
                "Local LLM",
                "AI capabilities",
                "Run LLM inference offline locally",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Requires cloud API",
                desk_detail="fusion-mlx local inference (Apple Silicon)",
            ),
            # ── Tool nodes ──
            Capability(
                "shell_exec",
                "Terminal execution",
                "Tool nodes",
                "Execute shell commands",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Native terminal",
                desk_detail="ShellExec node",
            ),
            Capability(
                "python_repl",
                "Python REPL",
                "Tool nodes",
                "Execute Python code",
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Executed via shell",
                desk_detail="PythonREPL node",
            ),
            Capability(
                "web_search",
                "Web search",
                "Tool nodes",
                "Search the internet for information",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Built-in search",
                desk_detail="WebSearch node",
            ),
            Capability(
                "fetch_url",
                "URL fetch",
                "Tool nodes",
                "Fetch web page content",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="curl/fetch",
                desk_detail="FetchURL node",
            ),
            Capability(
                "apply_edit",
                "File editing",
                "Tool nodes",
                "Precise search-and-replace of file content",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Native editing capability",
                desk_detail="ApplyEdit node",
            ),
            # ── Browser ──
            Capability(
                "browser",
                "Embedded browser",
                "Browser",
                "Embedded browser + automated extraction",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="No built-in browser",
                desk_detail="WKWebView BrowserOpen/Extract/Automate nodes",
            ),
            # ── Workflow engine ──
            Capability(
                "workflow_engine",
                "Workflow engine",
                "Workflow",
                "DAG orchestration/topological sort/data passing/parallel execution",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="No workflow concept",
                desk_detail="WorkflowEngine + node system",
            ),
            Capability(
                "template",
                "Template center",
                "Workflow",
                "Preset + industry templates",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="No template system",
                desk_detail="10 built-in + 10 industry templates",
            ),
            Capability(
                "nl_workflow",
                "NL workflow generation",
                "Workflow",
                "Natural language description → auto-generated workflow",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Requires manual orchestration",
                desk_detail="NLWorkflowGenerator",
            ),
            Capability(
                "scheduler",
                "Scheduled execution",
                "Workflow",
                "Cron-scheduled workflow execution",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="No scheduler",
                desk_detail="TaskScheduler + EnhancedScheduler",
            ),
            # ── Permissions/security ──
            Capability(
                "permission",
                "Permission model",
                "Permissions/security",
                "Fine-grained tool execution permission control",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="User confirmation mode",
                desk_detail="4-level permission model + rule persistence",
            ),
            Capability(
                "hook",
                "Hook system",
                "Permissions/security",
                "Intercept/modify/cancel before and after execution",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="No hook system",
                desk_detail="HookManager with 11 event types",
            ),
            # ── Integration protocols ──
            Capability(
                "mcp_server",
                "MCP server",
                "Integration protocols",
                "Serve as an MCP server for Claude Code/desktop clients",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="MCP client",
                desk_detail="14-tool MCP server + stdio/HTTP",
            ),
            Capability(
                "ipc_rpc",
                "IPC RPC",
                "Integration protocols",
                "Unix Domain Socket JSON-RPC communication",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="DeskRPCServer (UDS) + MCP stdio/HTTP",
                desk_detail="DeskRPCServer 103 methods",
            ),
            Capability(
                "sse_events",
                "SSE event stream",
                "Integration protocols",
                "Server-push realtime events",
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="EventEmitter + /sse endpoint (mcp_http)",
                desk_detail="EventEmitter + SSE endpoint",
            ),
            # ── Sessions/persistence ──
            Capability(
                "session",
                "Session persistence",
                "Sessions/persistence",
                "Save/restore/fork workflow execution state",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="No session concept",
                desk_detail="SessionStore SQLite",
            ),
            Capability(
                "cross_device",
                "Cross-device sync",
                "Sessions/persistence",
                "Sync workflows/state across devices",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="No sync",
                desk_detail="CrossDeviceSync WebSocket",
            ),
            # ── Offline/privacy ──
            Capability(
                "offline",
                "Fully offline",
                "Offline/privacy",
                "Runs without any network connection",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="Must be online to use the Claude API",
                desk_detail="Local fusion-mlx inference",
            ),
            Capability(
                "privacy",
                "Data privacy",
                "Offline/privacy",
                "Files/data never leave the machine",
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="Data sent to the cloud",
                desk_detail="100% local processing",
            ),
        ]
        for cap in defaults:
            self._capabilities[cap.id] = cap
        logger.info(f"Capability matrix initialized: {len(defaults)} capabilities")

    def add(self, cap: Capability) -> None:
        self._capabilities[cap.id] = cap

    def get(self, cap_id: str) -> Optional[Capability]:
        return self._capabilities.get(cap_id)

    def list_all(self) -> List[Capability]:
        return list(self._capabilities.values())

    def categories(self) -> List[str]:
        seen = []
        for c in self._capabilities.values():
            if c.category not in seen:
                seen.append(c.category)
        return seen

    def by_category(self, category: str) -> List[Capability]:
        return [c for c in self._capabilities.values() if c.category == category]

    def parity_score(self) -> float:
        cowork_has = sum(1 for c in self._capabilities.values() if c.cowork_level.value > 0)
        desk_has = sum(1 for c in self._capabilities.values() if c.desk_level.value > 0)
        if cowork_has == 0:
            return 1.0
        return round(desk_has / cowork_has, 2)

    def desk_unique_count(self) -> int:
        return sum(1 for c in self._capabilities.values() if c.desk_level.value > 0 and c.cowork_level.value == 0)

    def cowork_unique_count(self) -> int:
        return sum(1 for c in self._capabilities.values() if c.cowork_level.value > 0 and c.desk_level.value == 0)

    def summary(self) -> Dict[str, Any]:
        total = len(self._capabilities)
        desk_full = sum(1 for c in self._capabilities.values() if c.desk_level.value >= 2)
        desk_adv = sum(1 for c in self._capabilities.values() if c.desk_level.value >= 3)
        cowork_full = sum(1 for c in self._capabilities.values() if c.cowork_level.value >= 2)
        return {
            "total_capabilities": total,
            "desk_full_or_above": desk_full,
            "desk_advanced": desk_adv,
            "cowork_full_or_above": cowork_full,
            "desk_unique": self.desk_unique_count(),
            "cowork_unique": self.cowork_unique_count(),
            "parity_score": self.parity_score(),
            "categories": self.categories(),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "capabilities": {k: v.to_dict() for k, v in self._capabilities.items()},
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
