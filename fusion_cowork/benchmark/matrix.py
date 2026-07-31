"""功能对比矩阵 — 定义 Claude Cowork vs Fusion-Cowork 的能力维度与评分。"""

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
    """功能对比矩阵 — 管理 Capability 列表，计算对等率。"""

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}
        self._init_default()

    def _init_default(self) -> None:
        defaults = [
            # ── 桌面自动化 ──
            Capability("file_ops", "文件操作", "桌面自动化",
                       "读写/复制/移动/删除/批量重命名/分类",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="通过终端命令执行", desk_detail="原生节点 file_input/output/copy/move/delete/batch_rename/classifier/find"),
            Capability("desktop_clean", "桌面整理", "桌面自动化",
                       "按类型/规则自动整理桌面文件",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="终端命令编排", desk_detail="DesktopClean 节点 + AI分类"),
            Capability("download_organize", "下载整理", "桌面自动化",
                       "按类型/日期自动整理下载文件夹",
                       cowork_level=CapabilityLevel.PARTIAL, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="需手动编排命令", desk_detail="DownloadOrganizer 节点"),
            Capability("disk_clean", "磁盘清理", "桌面自动化",
                       "清理缓存/临时文件/大文件",
                       cowork_level=CapabilityLevel.PARTIAL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="需手写脚本", desk_detail="DiskCleaner 节点"),
            # ── macOS 集成 ──
            Capability("screen_capture", "截屏", "macOS 集成",
                       "桌面截图 + AI 分析",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="screencapture + AI", desk_detail="ScreenCapture 节点"),
            Capability("clipboard", "剪贴板", "macOS 集成",
                       "读写系统剪贴板",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="pbcopy/pbpaste", desk_detail="Clipboard 节点"),
            Capability("notification", "通知", "macOS 集成",
                       "发送系统通知",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="osascript notification", desk_detail="Notification 节点"),
            Capability("app_lifecycle", "应用管理", "macOS 集成",
                       "启动/退出/激活/列出应用",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="open/killall/osascript", desk_detail="AppLifecycle 节点"),
            Capability("ocr", "OCR 文字识别", "macOS 集成",
                       "图片/截屏文字识别",
                       cowork_level=CapabilityLevel.PARTIAL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="需安装 tesseract", desk_detail="Vision + MLX OCR 节点"),
            # ── AI 能力 ──
            Capability("ai_classify", "AI 分类", "AI 能力",
                       "基于内容的智能文件分类",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="Claude 直接分类", desk_detail="AIClassify 节点 + fusion-mlx"),
            Capability("ai_summarize", "AI 摘要", "AI 能力",
                       "文档内容智能摘要",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="Claude 直接总结", desk_detail="AISummarize 节点 + fusion-mlx"),
            Capability("ai_rename", "AI 命名", "AI 能力",
                       "基于内容的智能文件命名",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="Claude 直接命名", desk_detail="AIGenerateName 节点"),
            Capability("local_llm", "本地大模型", "AI 能力",
                       "离线本地运行 LLM 推理",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="需云端 API", desk_detail="fusion-mlx 本地推理 (Apple Silicon)"),
            # ── 工具节点 ──
            Capability("shell_exec", "终端执行", "工具节点",
                       "执行 shell 命令",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="原生终端", desk_detail="ShellExec 节点"),
            Capability("python_repl", "Python REPL", "工具节点",
                       "执行 Python 代码",
                       cowork_level=CapabilityLevel.PARTIAL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="通过 shell 执行", desk_detail="PythonREPL 节点"),
            Capability("web_search", "网页搜索", "工具节点",
                       "搜索互联网信息",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="内置搜索", desk_detail="WebSearch 节点"),
            Capability("fetch_url", "URL 获取", "工具节点",
                       "获取网页内容",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="curl/fetch", desk_detail="FetchURL 节点"),
            Capability("apply_edit", "文件编辑", "工具节点",
                       "精确搜索替换文件内容",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.FULL,
                       cowork_detail="原生编辑能力", desk_detail="ApplyEdit 节点"),
            # ── 浏览器 ──
            Capability("browser", "嵌入式浏览器", "浏览器",
                       "内嵌浏览器 + 自动化提取",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="无内置浏览器", desk_detail="WKWebView BrowserOpen/Extract/Automate 节点"),
            # ── 工作流引擎 ──
            Capability("workflow_engine", "工作流引擎", "工作流",
                       "DAG 编排/拓扑排序/数据传递/并行执行",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="无工作流概念", desk_detail="WorkflowEngine + 节点系统"),
            Capability("template", "模板中心", "工作流",
                       "预置 + 行业模板",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="无模板系统", desk_detail="10 内置 + 10 行业模板"),
            Capability("nl_workflow", "NL 生成工作流", "工作流",
                       "自然语言描述 → 自动生成工作流",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="需手动编排", desk_detail="NLWorkflowGenerator"),
            Capability("scheduler", "定时调度", "工作流",
                       "Cron 定时执行工作流",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="无调度器", desk_detail="TaskScheduler + EnhancedScheduler"),
            # ── 权限/安全 ──
            Capability("permission", "权限模型", "权限/安全",
                       "细粒度工具执行权限控制",
                       cowork_level=CapabilityLevel.FULL, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="用户确认模式", desk_detail="4级权限模型 + 规则持久化"),
            Capability("hook", "Hook 系统", "权限/安全",
                       "执行前后拦截/修改/取消",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="无 Hook 系统", desk_detail="11 事件类型 HookManager"),
            # ── 集成协议 ──
            Capability("mcp_server", "MCP 服务端", "集成协议",
                       "作为 MCP 服务供 Claude Code/桌面调用",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="MCP 客户端", desk_detail="14 工具 MCP 服务端 + stdio/HTTP"),
            Capability("ipc_rpc", "IPC RPC", "集成协议",
                       "Unix Domain Socket JSON-RPC 通信",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="无 IPC 服务", desk_detail="DeskRPCServer 24 方法"),
            Capability("sse_events", "SSE 事件流", "集成协议",
                       "服务端推送实时事件",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="无 SSE", desk_detail="EventEmitter + SSE 端点"),
            # ── 会话/持久化 ──
            Capability("session", "会话持久化", "会话/持久化",
                       "工作流执行状态保存/恢复/分叉",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="无会话概念", desk_detail="SessionStore SQLite"),
            Capability("cross_device", "跨设备同步", "会话/持久化",
                       "多设备间工作流/状态同步",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.FULL,
                       cowork_detail="无同步", desk_detail="CrossDeviceSync WebSocket"),
            # ── 离线/隐私 ──
            Capability("offline", "完全离线", "离线/隐私",
                       "无需任何网络连接即可运行",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="必须联网使用 Claude API", desk_detail="本地 fusion-mlx 推理"),
            Capability("privacy", "数据隐私", "离线/隐私",
                       "文件/数据不离开本机",
                       cowork_level=CapabilityLevel.NONE, desk_level=CapabilityLevel.ADVANCED,
                       cowork_detail="数据发送到云端", desk_detail="100% 本地处理"),
        ]
        for cap in defaults:
            self._capabilities[cap.id] = cap
        logger.info(f"功能矩阵初始化: {len(defaults)} 项能力")

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
        return sum(1 for c in self._capabilities.values()
                   if c.desk_level.value > 0 and c.cowork_level.value == 0)

    def cowork_unique_count(self) -> int:
        return sum(1 for c in self._capabilities.values()
                   if c.cowork_level.value > 0 and c.desk_level.value == 0)

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
