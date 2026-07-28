"""Fusion-Desk — macOS 原生、纯本地离线、零代码桌面智能自动化平台。

产品定位：让 Mac 自己干活，本地 AI 全自动桌面办公。

架构模式吸纳自 Squish 的 Lazy Import 机制（__getattr__ 延迟导入）：
- import fusion_desk 保持快速，不加载任何节点模块
- 首次访问属性时自动加载对应模块
- 同时保留显式 import 的兼容性
"""

from __future__ import annotations

__version__ = "0.1.0"
__app_name__ = "Fusion-Desk"

# ── 节点工具名称映射表（吸纳自 Squish tool_name_map.py） ──
# 用户友好的中文名称 → 后端节点名称
# 用于 AI 生成工作流时的节点名称解析
NODE_NAME_ALIASES: dict[str, str] = {
    # 桌面清理
    "桌面清理": "desktop_clean",
    "整理桌面": "desktop_clean",
    "桌面规整": "desktop_clean",
    # 下载整理
    "下载整理": "download_organizer",
    "下载归档": "download_organizer",
    "整理下载": "download_organizer",
    # 文件分类
    "文件分类": "file_classifier",
    "分类文件": "file_classifier",
    "按类型分类": "file_classifier",
    # AI 分类
    "AI分类": "ai_classify",
    "智能分类": "ai_classify",
    "AI文件分类": "ai_classify",
    # AI 摘要
    "AI摘要": "ai_summarize",
    "文档摘要": "ai_summarize",
    "批量总结": "ai_summarize",
    # AI 重命名
    "AI重命名": "ai_generate_name",
    "智能重命名": "ai_generate_name",
    "批量重命名": "file_batch_rename",
    # 磁盘清理
    "磁盘清理": "disk_cleaner",
    "清理磁盘": "disk_cleaner",
    "垃圾清理": "disk_cleaner",
    # 文件监听
    "文件监听": "file_watcher",
    "监听目录": "file_watcher",
    # 文件操作
    "复制文件": "file_copy",
    "备份文件": "file_copy",
    "移动文件": "file_move",
    "删除文件": "file_delete",
    "查找文件": "file_find",
    "搜索文件": "file_find",
    # 输入输出
    "读取文件": "file_input",
    "文件输入": "file_input",
    "写入文件": "file_output",
    "文件输出": "file_output",
    # 逻辑
    "条件过滤": "filter",
    "过滤": "filter",
    "循环处理": "loop",
    "循环": "loop",
    "合并数据": "merge",
    "数据合并": "merge",
    # 工具（新增）
    "运行命令": "shell_exec",
    "执行命令": "shell_exec",
    "终端命令": "shell_exec",
    "运行Python": "python_repl",
    "执行Python": "python_repl",
    "搜索网页": "web_search",
    "联网搜索": "web_search",
    "获取网页": "fetch_url",
    "下载网页": "fetch_url",
    "应用编辑": "apply_edit",
    "编辑文件": "apply_edit",
    # 浏览器
    "打开浏览器": "browser_open",
    "浏览器打开": "browser_open",
    "提取网页": "browser_extract",
    "网页提取": "browser_extract",
    "网页自动化": "browser_automate",
    "浏览器自动化": "browser_automate",
    # Chrome CDP
    "CDP导航": "cdp_navigate",
    "CDP快照": "cdp_snapshot",
    "CDP点击": "cdp_click",
    "CDP填写": "cdp_fill",
    "CDP批量填写": "cdp_fill_form",
    "CDP截图": "cdp_screenshot",
    "CDP执行JS": "cdp_evaluate",
    "CDP设备模拟": "cdp_emulate",
    "CDP网络监控": "cdp_network",
    "CDP控制台": "cdp_console",
}

# ── Lazy Import 注册表（吸纳自 Squish 的 __getattr__ 机制） ──
# 键：公开属性名，值：模块路径
# 首次访问时自动导入对应模块，保持 import 快速
_LAZY_IMPORTS: dict[str, str] = {
    # 引擎核心
    "Workflow": "fusion_desk.engine.workflow",
    "WorkflowEngine": "fusion_desk.engine.workflow",
    "WorkflowStatus": "fusion_desk.engine.workflow",
    "WorkflowExecution": "fusion_desk.engine.workflow",
    "Edge": "fusion_desk.engine.workflow",
    "WorkflowStep": "fusion_desk.engine.workflow",
    "BaseNode": "fusion_desk.engine.node",
    "NodeConfig": "fusion_desk.engine.node",
    "NodeResult": "fusion_desk.engine.node",
    "NodeStatus": "fusion_desk.engine.node",
    "NodeCategory": "fusion_desk.engine.node",
    "NodeRegistry": "fusion_desk.engine.node",
    "register_node": "fusion_desk.engine.node",
    "coerce_param": "fusion_desk.engine.node",
    "coerce_params": "fusion_desk.engine.node",
    "TaskScheduler": "fusion_desk.engine.scheduler",
    "ScheduledTask": "fusion_desk.engine.scheduler",
    "TaskStatus": "fusion_desk.engine.scheduler",
    # macOS 节点
    "DesktopCleanNode": "fusion_desk.nodes.macos",
    "DownloadOrganizerNode": "fusion_desk.nodes.macos",
    "FileClassifierNode": "fusion_desk.nodes.macos",
    "FileBatchRenameNode": "fusion_desk.nodes.macos",
    "DiskCleanerNode": "fusion_desk.nodes.macos",
    "FileWatcherNode": "fusion_desk.nodes.macos",
    "FileCopyNode": "fusion_desk.nodes.macos",
    "FileMoveNode": "fusion_desk.nodes.macos",
    "FileDeleteNode": "fusion_desk.nodes.macos",
    "FileFindNode": "fusion_desk.nodes.macos",
    # AI 节点
    "AIClassifyNode": "fusion_desk.nodes.ai",
    "AISummarizeNode": "fusion_desk.nodes.ai",
    "AIGenerateNameNode": "fusion_desk.nodes.ai",
    # IO 节点
    "FileInputNode": "fusion_desk.nodes.io",
    "FileOutputNode": "fusion_desk.nodes.io",
    # 逻辑节点
    "FilterNode": "fusion_desk.nodes.logic",
    "LoopNode": "fusion_desk.nodes.logic",
    "MergeNode": "fusion_desk.nodes.logic",
    # 工具节点（新增）
    "ShellExecNode": "fusion_desk.nodes.tools",
    "PythonREPLNode": "fusion_desk.nodes.tools",
    "WebSearchNode": "fusion_desk.nodes.tools",
    "FetchURLNode": "fusion_desk.nodes.tools",
    "ApplyEditNode": "fusion_desk.nodes.tools",
    # 浏览器节点（新增）
    "BrowserOpenNode": "fusion_desk.nodes.browser",
    "BrowserExtractNode": "fusion_desk.nodes.browser",
    "BrowserAutomateNode": "fusion_desk.nodes.browser",
    "BrowserClient": "fusion_desk.nodes.browser",
    "BrowserManager": "fusion_desk.nodes.browser",
    # V0.2 增强调度
    "EnhancedScheduler": "fusion_desk.engine.enhanced_scheduler",
    "TaskExecution": "fusion_desk.engine.enhanced_scheduler",
    "TaskDependency": "fusion_desk.engine.enhanced_scheduler",
    # V0.2 AI 优化
    "WorkflowOptimizer": "fusion_desk.engine.optimizer",
    "OptimizationSuggestion": "fusion_desk.engine.optimizer",
    "WorkflowAnalysis": "fusion_desk.engine.optimizer",
    # V0.2 报告生成
    "ReportGenerator": "fusion_desk.report.report_generator",
    "ReportConfig": "fusion_desk.report.report_generator",
    # V0.3 多智能体
    "AgentOrchestrator": "fusion_desk.orchestrator.orchestrator",
    "Agent": "fusion_desk.orchestrator.orchestrator",
    "AgentRole": "fusion_desk.orchestrator.orchestrator",
    "AgentTask": "fusion_desk.orchestrator.orchestrator",
    # V0.3 跨设备协同
    "CrossDeviceSync": "fusion_desk.server.sync",
    "Device": "fusion_desk.server.sync",
    "SyncMessage": "fusion_desk.server.sync",
    # Claude Cowork 对标节点
    "ScreenCaptureNode": "fusion_desk.nodes.macos",
    "ClipboardNode": "fusion_desk.nodes.macos",
    "NotificationNode": "fusion_desk.nodes.macos",
    "AppLifecycleNode": "fusion_desk.nodes.macos",
    "OCRNode": "fusion_desk.nodes.macos",
    # MCP Server
    "MCPServer": "fusion_desk.server.mcp_server",
    # AI 客户端
    "FusionMLXClient": "fusion_desk.ai",
    "KBClient": "fusion_desk.ai",
    "NLWorkflowGenerator": "fusion_desk.ai",
    # 模板
    "TemplateManager": "fusion_desk.templates",
    # M3 插件系统
    "PluginManifest": "fusion_desk.plugins.manifest",
    "PluginLoader": "fusion_desk.plugins.loader",
    # M3 技能机制
    "Skill": "fusion_desk.skills.registry",
    "SkillRegistry": "fusion_desk.skills.registry",
    "register_builtin_skills": "fusion_desk.skills.builtin",
    "BUILTIN_SKILLS": "fusion_desk.skills.builtin",
    # M3 Chrome CDP
    "CDPClient": "fusion_desk.nodes.browser",
    "CDPNavigateNode": "fusion_desk.nodes.browser",
    "CDPSnapshotNode": "fusion_desk.nodes.browser",
    "CDPClickNode": "fusion_desk.nodes.browser",
    "CDPFillNode": "fusion_desk.nodes.browser",
    "CDPFillFormNode": "fusion_desk.nodes.browser",
    "CDPScreenshotNode": "fusion_desk.nodes.browser",
    "CDPEvaluateNode": "fusion_desk.nodes.browser",
    "CDPEmulateNode": "fusion_desk.nodes.browser",
    "CDPNetworkNode": "fusion_desk.nodes.browser",
    "CDPConsoleNode": "fusion_desk.nodes.browser",
}

_lazy_cache: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """延迟加载模块（吸纳自 Squish 的 __getattr__ 模式）。

    仅在首次访问时导入对应模块，之后缓存结果。
    """
    if name in _lazy_cache:
        return _lazy_cache[name]
    if name in _LAZY_IMPORTS:
        import importlib
        mod_name = _LAZY_IMPORTS[name]
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, name)
            _lazy_cache[name] = obj
            return obj
        except (ImportError, AttributeError) as exc:
            raise AttributeError(
                f"module 'fusion_desk' has no attribute {name!r} "
                f"(lazy import {mod_name!r} failed: {exc})"
            ) from None
    raise AttributeError(f"module 'fusion_desk' has no attribute {name!r}")


def __dir__() -> list[str]:
    """列出所有可访问的属性。"""
    return sorted(set([*dir(type("", (), {})), *__all__, *_LAZY_IMPORTS.keys()]))


__all__ = [
    "__version__",
    "__app_name__",
    "NODE_NAME_ALIASES",
    *_LAZY_IMPORTS.keys(),
]