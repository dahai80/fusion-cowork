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
    """Capability comparison matrix — manages the capability list and computes the parity score."""

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}
        self._init_default()

    def _init_default(self) -> None:
        defaults = [
            # ── Desktop automation ──
            Capability(
                "file_ops",
                "文件操作",  # i18n: user-facing
                "桌面自动化",  # i18n: user-facing
                "读写/复制/移动/删除/批量重命名/分类",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="通过终端命令执行",  # i18n: user-facing
                desk_detail="原生节点 file_input/output/copy/move/delete/batch_rename/classifier/find",  # i18n: user-facing
            ),
            Capability(
                "desktop_clean",
                "桌面整理",  # i18n: user-facing
                "桌面自动化",  # i18n: user-facing
                "按类型/规则自动整理桌面文件",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="终端命令编排",  # i18n: user-facing
                desk_detail="DesktopClean 节点 + AI分类",  # i18n: user-facing
            ),
            Capability(
                "download_organize",
                "下载整理",  # i18n: user-facing
                "桌面自动化",  # i18n: user-facing
                "按类型/日期自动整理下载文件夹",  # i18n: user-facing
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="需手动编排命令",  # i18n: user-facing
                desk_detail="DownloadOrganizer 节点",  # i18n: user-facing
            ),
            Capability(
                "disk_clean",
                "磁盘清理",  # i18n: user-facing
                "桌面自动化",  # i18n: user-facing
                "清理缓存/临时文件/大文件",  # i18n: user-facing
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="需手写脚本",  # i18n: user-facing
                desk_detail="DiskCleaner 节点",  # i18n: user-facing
            ),
            # ── macOS integration ──
            Capability(
                "screen_capture",
                "截屏",  # i18n: user-facing
                "macOS 集成",  # i18n: user-facing
                "桌面截图 + AI 分析",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="screencapture + AI",
                desk_detail="ScreenCapture 节点",  # i18n: user-facing
            ),
            Capability(
                "clipboard",
                "剪贴板",  # i18n: user-facing
                "macOS 集成",  # i18n: user-facing
                "读写系统剪贴板",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="pbcopy/pbpaste",
                desk_detail="Clipboard 节点",  # i18n: user-facing
            ),
            Capability(
                "notification",
                "通知",  # i18n: user-facing
                "macOS 集成",  # i18n: user-facing
                "发送系统通知",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="osascript notification",
                desk_detail="Notification 节点",  # i18n: user-facing
            ),
            Capability(
                "app_lifecycle",
                "应用管理",  # i18n: user-facing
                "macOS 集成",  # i18n: user-facing
                "启动/退出/激活/列出应用",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="open/killall/osascript",
                desk_detail="AppLifecycle 节点",  # i18n: user-facing
            ),
            Capability(
                "ocr",
                "OCR 文字识别",  # i18n: user-facing
                "macOS 集成",  # i18n: user-facing
                "图片/截屏文字识别",  # i18n: user-facing
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="需安装 tesseract",  # i18n: user-facing
                desk_detail="Vision + MLX OCR 节点",  # i18n: user-facing
            ),
            # ── AI capabilities ──
            Capability(
                "ai_classify",
                "AI 分类",  # i18n: user-facing
                "AI 能力",  # i18n: user-facing
                "基于内容的智能文件分类",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude 直接分类",  # i18n: user-facing
                desk_detail="AIClassify 节点 + fusion-mlx",  # i18n: user-facing
            ),
            Capability(
                "ai_summarize",
                "AI 摘要",  # i18n: user-facing
                "AI 能力",  # i18n: user-facing
                "文档内容智能摘要",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude 直接总结",  # i18n: user-facing
                desk_detail="AISummarize 节点 + fusion-mlx",  # i18n: user-facing
            ),
            Capability(
                "ai_rename",
                "AI 命名",  # i18n: user-facing
                "AI 能力",  # i18n: user-facing
                "基于内容的智能文件命名",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="Claude 直接命名",  # i18n: user-facing
                desk_detail="AIGenerateName 节点",  # i18n: user-facing
            ),
            Capability(
                "local_llm",
                "本地大模型",  # i18n: user-facing
                "AI 能力",  # i18n: user-facing
                "离线本地运行 LLM 推理",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="需云端 API",  # i18n: user-facing
                desk_detail="fusion-mlx 本地推理 (Apple Silicon)",  # i18n: user-facing
            ),
            # ── Tool nodes ──
            Capability(
                "shell_exec",
                "终端执行",  # i18n: user-facing
                "工具节点",  # i18n: user-facing
                "执行 shell 命令",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="原生终端",  # i18n: user-facing
                desk_detail="ShellExec 节点",  # i18n: user-facing
            ),
            Capability(
                "python_repl",
                "Python REPL",
                "工具节点",  # i18n: user-facing
                "执行 Python 代码",  # i18n: user-facing
                cowork_level=CapabilityLevel.PARTIAL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="通过 shell 执行",  # i18n: user-facing
                desk_detail="PythonREPL 节点",  # i18n: user-facing
            ),
            Capability(
                "web_search",
                "网页搜索",  # i18n: user-facing
                "工具节点",  # i18n: user-facing
                "搜索互联网信息",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="内置搜索",  # i18n: user-facing
                desk_detail="WebSearch 节点",  # i18n: user-facing
            ),
            Capability(
                "fetch_url",
                "URL 获取",  # i18n: user-facing
                "工具节点",  # i18n: user-facing
                "获取网页内容",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="curl/fetch",
                desk_detail="FetchURL 节点",  # i18n: user-facing
            ),
            Capability(
                "apply_edit",
                "文件编辑",  # i18n: user-facing
                "工具节点",  # i18n: user-facing
                "精确搜索替换文件内容",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="原生编辑能力",  # i18n: user-facing
                desk_detail="ApplyEdit 节点",  # i18n: user-facing
            ),
            # ── Browser ──
            Capability(
                "browser",
                "嵌入式浏览器",  # i18n: user-facing
                "浏览器",  # i18n: user-facing
                "内嵌浏览器 + 自动化提取",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="无内置浏览器",  # i18n: user-facing
                desk_detail="WKWebView BrowserOpen/Extract/Automate 节点",  # i18n: user-facing
            ),
            # ── Workflow engine ──
            Capability(
                "workflow_engine",
                "工作流引擎",  # i18n: user-facing
                "工作流",  # i18n: user-facing
                "DAG 编排/拓扑排序/数据传递/并行执行",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="无工作流概念",  # i18n: user-facing
                desk_detail="WorkflowEngine + 节点系统",  # i18n: user-facing
            ),
            Capability(
                "template",
                "模板中心",  # i18n: user-facing
                "工作流",  # i18n: user-facing
                "预置 + 行业模板",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="无模板系统",  # i18n: user-facing
                desk_detail="10 内置 + 10 行业模板",  # i18n: user-facing
            ),
            Capability(
                "nl_workflow",
                "NL 生成工作流",  # i18n: user-facing
                "工作流",  # i18n: user-facing
                "自然语言描述 → 自动生成工作流",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="需手动编排",  # i18n: user-facing
                desk_detail="NLWorkflowGenerator",
            ),
            Capability(
                "scheduler",
                "定时调度",  # i18n: user-facing
                "工作流",  # i18n: user-facing
                "Cron 定时执行工作流",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="无调度器",  # i18n: user-facing
                desk_detail="TaskScheduler + EnhancedScheduler",
            ),
            # ── Permissions/security ──
            Capability(
                "permission",
                "权限模型",  # i18n: user-facing
                "权限/安全",  # i18n: user-facing
                "细粒度工具执行权限控制",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="用户确认模式",  # i18n: user-facing
                desk_detail="4级权限模型 + 规则持久化",  # i18n: user-facing
            ),
            Capability(
                "hook",
                "Hook 系统",  # i18n: user-facing
                "权限/安全",  # i18n: user-facing
                "执行前后拦截/修改/取消",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="无 Hook 系统",  # i18n: user-facing
                desk_detail="11 事件类型 HookManager",  # i18n: user-facing
            ),
            # ── Integration protocols ──
            Capability(
                "mcp_server",
                "MCP 服务端",  # i18n: user-facing
                "集成协议",  # i18n: user-facing
                "作为 MCP 服务供 Claude Code/桌面调用",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="MCP 客户端",  # i18n: user-facing
                desk_detail="14 工具 MCP 服务端 + stdio/HTTP",  # i18n: user-facing
            ),
            Capability(
                "ipc_rpc",
                "IPC RPC",
                "集成协议",  # i18n: user-facing
                "Unix Domain Socket JSON-RPC 通信",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="DeskRPCServer (UDS) + MCP stdio/HTTP",
                desk_detail="DeskRPCServer 103 方法",  # i18n: user-facing
            ),
            Capability(
                "sse_events",
                "SSE 事件流",  # i18n: user-facing
                "集成协议",  # i18n: user-facing
                "服务端推送实时事件",  # i18n: user-facing
                cowork_level=CapabilityLevel.FULL,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="EventEmitter + /sse 端点 (mcp_http)",  # i18n: user-facing
                desk_detail="EventEmitter + SSE 端点",  # i18n: user-facing
            ),
            # ── Sessions/persistence ──
            Capability(
                "session",
                "会话持久化",  # i18n: user-facing
                "会话/持久化",  # i18n: user-facing
                "工作流执行状态保存/恢复/分叉",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="无会话概念",  # i18n: user-facing
                desk_detail="SessionStore SQLite",
            ),
            Capability(
                "cross_device",
                "跨设备同步",  # i18n: user-facing
                "会话/持久化",  # i18n: user-facing
                "多设备间工作流/状态同步",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.FULL,
                cowork_detail="无同步",  # i18n: user-facing
                desk_detail="CrossDeviceSync WebSocket",
            ),
            # ── Offline/privacy ──
            Capability(
                "offline",
                "完全离线",  # i18n: user-facing
                "离线/隐私",  # i18n: user-facing
                "无需任何网络连接即可运行",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="必须联网使用 Claude API",  # i18n: user-facing
                desk_detail="本地 fusion-mlx 推理",  # i18n: user-facing
            ),
            Capability(
                "privacy",
                "数据隐私",  # i18n: user-facing
                "离线/隐私",  # i18n: user-facing
                "文件/数据不离开本机",  # i18n: user-facing
                cowork_level=CapabilityLevel.NONE,
                desk_level=CapabilityLevel.ADVANCED,
                cowork_detail="数据发送到云端",  # i18n: user-facing
                desk_detail="100% 本地处理",  # i18n: user-facing
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
