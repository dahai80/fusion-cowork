"""Fusion-Cowork — macOS-native, fully local offline, zero-code desktop intelligent automation platform.

Product positioning: let the Mac do the work itself — local AI fully automating desktop office tasks.

The architecture adopts the Squish Lazy Import pattern (__getattr__ deferred imports):
- importing fusion_cowork stays fast; no node modules are loaded
- the corresponding module loads automatically on first attribute access
- explicit imports remain compatible
"""

from __future__ import annotations

__version__ = "0.5.152"
__app_name__ = "Fusion-Cowork"

# ── Node tool name mapping table (adopted from Squish tool_name_map.py) ──
# User-friendly Chinese names -> backend node names
# Used for node name resolution when AI generates workflows
NODE_NAME_ALIASES: dict[str, str] = {
    # Desktop cleanup
    "桌面清理": "desktop_clean",  # i18n: user-facing
    "整理桌面": "desktop_clean",  # i18n: user-facing
    "桌面规整": "desktop_clean",  # i18n: user-facing
    # Download organization
    "下载整理": "download_organizer",  # i18n: user-facing
    "下载归档": "download_organizer",  # i18n: user-facing
    "整理下载": "download_organizer",  # i18n: user-facing
    # File classification
    "文件分类": "file_classifier",  # i18n: user-facing
    "分类文件": "file_classifier",  # i18n: user-facing
    "按类型分类": "file_classifier",  # i18n: user-facing
    # AI classification
    "AI分类": "ai_classify",  # i18n: user-facing
    "智能分类": "ai_classify",  # i18n: user-facing
    "AI文件分类": "ai_classify",  # i18n: user-facing
    # AI summarization
    "AI摘要": "ai_summarize",  # i18n: user-facing
    "文档摘要": "ai_summarize",  # i18n: user-facing
    "批量总结": "ai_summarize",  # i18n: user-facing
    # AI renaming
    "AI重命名": "ai_generate_name",  # i18n: user-facing
    "智能重命名": "ai_generate_name",  # i18n: user-facing
    "批量重命名": "file_batch_rename",  # i18n: user-facing
    # fusion-browser engine (issue #162)
    "融合浏览器": "fusion_browser",  # i18n: user-facing
    "fusion浏览器": "fusion_browser",  # i18n: user-facing
    # Disk cleanup
    "磁盘清理": "disk_cleaner",  # i18n: user-facing
    "清理磁盘": "disk_cleaner",  # i18n: user-facing
    "垃圾清理": "disk_cleaner",  # i18n: user-facing
    # File watching
    "文件监听": "file_watcher",  # i18n: user-facing
    "监听目录": "file_watcher",  # i18n: user-facing
    # File operations
    "复制文件": "file_copy",  # i18n: user-facing
    "备份文件": "file_copy",  # i18n: user-facing
    "移动文件": "file_move",  # i18n: user-facing
    "删除文件": "file_delete",  # i18n: user-facing
    "查找文件": "file_find",  # i18n: user-facing
    "搜索文件": "file_find",  # i18n: user-facing
    # Input/output
    "读取文件": "file_input",  # i18n: user-facing
    "文件输入": "file_input",  # i18n: user-facing
    "写入文件": "file_output",  # i18n: user-facing
    "文件输出": "file_output",  # i18n: user-facing
    "写入Excel": "xlsx_write",  # i18n: user-facing
    "Excel写入": "xlsx_write",  # i18n: user-facing
    "生成Excel": "xlsx_write",  # i18n: user-facing
    "写入PPT": "pptx_write",  # i18n: user-facing
    "PPT写入": "pptx_write",  # i18n: user-facing
    "生成PPT": "pptx_write",  # i18n: user-facing
    # Logic
    "条件过滤": "filter",  # i18n: user-facing
    "过滤": "filter",  # i18n: user-facing
    "循环处理": "loop",  # i18n: user-facing
    "循环": "loop",  # i18n: user-facing
    "合并数据": "merge",  # i18n: user-facing
    "数据合并": "merge",  # i18n: user-facing
    "等待事件": "wait_for_event",  # i18n: user-facing
    "事件等待": "wait_for_event",  # i18n: user-facing
    # Tools (new)
    "运行命令": "shell_exec",  # i18n: user-facing
    "执行命令": "shell_exec",  # i18n: user-facing
    "终端命令": "shell_exec",  # i18n: user-facing
    "运行Python": "python_repl",  # i18n: user-facing
    "执行Python": "python_repl",  # i18n: user-facing
    "搜索网页": "web_search",  # i18n: user-facing
    "联网搜索": "web_search",  # i18n: user-facing
    "获取网页": "fetch_url",  # i18n: user-facing
    "下载网页": "fetch_url",  # i18n: user-facing
    "应用编辑": "apply_edit",  # i18n: user-facing
    "编辑文件": "apply_edit",  # i18n: user-facing
    # Browser
    "打开浏览器": "browser_open",  # i18n: user-facing
    "浏览器打开": "browser_open",  # i18n: user-facing
    "提取网页": "browser_extract",  # i18n: user-facing
    "网页提取": "browser_extract",  # i18n: user-facing
    "网页自动化": "browser_automate",  # i18n: user-facing
    "浏览器自动化": "browser_automate",  # i18n: user-facing
    # Chrome CDP
    "CDP导航": "cdp_navigate",  # i18n: user-facing
    "CDP快照": "cdp_snapshot",  # i18n: user-facing
    "CDP点击": "cdp_click",  # i18n: user-facing
    "CDP填写": "cdp_fill",  # i18n: user-facing
    "CDP批量填写": "cdp_fill_form",  # i18n: user-facing
    "CDP截图": "cdp_screenshot",  # i18n: user-facing
    "CDP执行JS": "cdp_evaluate",  # i18n: user-facing
    "CDP设备模拟": "cdp_emulate",  # i18n: user-facing
    "CDP网络监控": "cdp_network",  # i18n: user-facing
    "CDP控制台": "cdp_console",  # i18n: user-facing
    # M4 Computer Use
    "鼠标移动": "mouse_move",  # i18n: user-facing
    "鼠标点击": "mouse_click",  # i18n: user-facing
    "键盘输入": "keyboard_type",  # i18n: user-facing
    "键盘快捷键": "keyboard_shortcut",  # i18n: user-facing
    "Computer Use": "computer_use_loop",
    # Fusion ecosystem interop
    "微调": "fusion_trainer",  # i18n: user-facing
    "模型微调": "fusion_trainer",  # i18n: user-facing
    "训练模型": "fusion_trainer",  # i18n: user-facing
    # Memory interop (fusion-memory)
    "记忆提交": "memory_commit",  # i18n: user-facing
    "提交记忆": "memory_commit",  # i18n: user-facing
    "记忆检索": "memory_retrieve",  # i18n: user-facing
    "召回记忆": "memory_retrieve",  # i18n: user-facing
    # Studio interop (fusion-agent-studio, issue #133)
    "运行Studio": "studio_run",  # i18n: user-facing
    "Studio运行": "studio_run",  # i18n: user-facing
    # Agentic Office (fusion-office office-api + artifacts-engine, issue #614)
    "办公文档": "office_document",  # i18n: user-facing
    "文档生成": "office_document",  # i18n: user-facing
    "文档解析": "office_document",  # i18n: user-facing
    "办公表格": "office_spreadsheet",  # i18n: user-facing
    "表格解析": "office_spreadsheet",  # i18n: user-facing
    "办公幻灯片": "office_slides",  # i18n: user-facing
    "幻灯片生成": "office_slides",  # i18n: user-facing
    "成果入库": "office_artifact",  # i18n: user-facing
    "工件入库": "office_artifact",  # i18n: user-facing
    # Team interop (agent-studio teams.*, issue #488)
    "团队调用": "team_call",  # i18n: user-facing
    # Decision gate (fusion-mlx laya decision head, issue #517)
    "决策门": "decision_gate",  # i18n: user-facing
    "智能决策": "decision_gate",  # i18n: user-facing
    "团队任务": "team_call",  # i18n: user-facing
    # Deploy execution (issue #489)
    "应用部署": "deploy_service",  # i18n: user-facing
    "部署服务": "deploy_service",  # i18n: user-facing
    # Sub-workflow (issue #462)
    "子工作流": "sub_workflow",  # i18n: user-facing
    "工作流嵌套": "sub_workflow",  # i18n: user-facing
    # MCP client (external MCP tool servers, issue #185)
    "MCP工具": "mcp_client",  # i18n: user-facing
    "MCP调用": "mcp_client",  # i18n: user-facing
    "调用MCP": "mcp_client",  # i18n: user-facing
    # Demo contract gate (issue #452)
    "演示契约": "demo_contract",  # i18n: user-facing
    "演示验收": "demo_contract",  # i18n: user-facing
    # Coordinator fanout / session fork (issue #467)
    "协调分发": "coordinator_fanout",  # i18n: user-facing
    "子代理分发": "coordinator_fanout",  # i18n: user-facing
    "会话分叉": "session_fork",  # i18n: user-facing
    "会话分叉并行": "session_fork",  # i18n: user-facing
    # Ladder orchestration (issue #470)
    "阶梯编排": "workflow_ladder",  # i18n: user-facing
    "工作流阶梯": "workflow_ladder",  # i18n: user-facing
    # Objective verification (issue #480)
    "目标校验": "verify_objective",  # i18n: user-facing
    "交付验证": "verify_objective",  # i18n: user-facing
}

# ── Lazy import registry (adopted from the Squish __getattr__ pattern) ──
# Key: public attribute name; value: module path
# The module is imported automatically on first access, keeping import fast
_LAZY_IMPORTS: dict[str, str] = {
    # Engine core
    "Workflow": "fusion_cowork.engine.workflow",
    "WorkflowEngine": "fusion_cowork.engine.workflow",
    "WorkflowStatus": "fusion_cowork.engine.workflow",
    "WorkflowExecution": "fusion_cowork.engine.workflow",
    "Edge": "fusion_cowork.engine.workflow",
    "WorkflowStep": "fusion_cowork.engine.workflow",
    "RunGate": "fusion_cowork.engine.run_gate",
    "ApprovalJournal": "fusion_cowork.engine.approval_journal",
    "BaseNode": "fusion_cowork.engine.node",
    "NodeConfig": "fusion_cowork.engine.node",
    "NodeResult": "fusion_cowork.engine.node",
    "NodeStatus": "fusion_cowork.engine.node",
    "NodeCategory": "fusion_cowork.engine.node",
    "NodeRegistry": "fusion_cowork.engine.node",
    "register_node": "fusion_cowork.engine.node",
    "coerce_param": "fusion_cowork.engine.node",
    "coerce_params": "fusion_cowork.engine.node",
    "TaskScheduler": "fusion_cowork.engine.scheduler",
    "RunStateStore": "fusion_cowork.engine.run_persistence",
    "ScheduledTask": "fusion_cowork.engine.scheduler",
    "TaskStatus": "fusion_cowork.engine.scheduler",
    # macOS nodes
    "DesktopCleanNode": "fusion_cowork.nodes.macos",
    "DownloadOrganizerNode": "fusion_cowork.nodes.macos",
    "FileClassifierNode": "fusion_cowork.nodes.macos",
    "FileBatchRenameNode": "fusion_cowork.nodes.macos",
    "DiskCleanerNode": "fusion_cowork.nodes.macos",
    "FileWatcherNode": "fusion_cowork.nodes.macos",
    "FileCopyNode": "fusion_cowork.nodes.macos",
    "FileMoveNode": "fusion_cowork.nodes.macos",
    "FileDeleteNode": "fusion_cowork.nodes.macos",
    "FileFindNode": "fusion_cowork.nodes.macos",
    # AI nodes
    "AIClassifyNode": "fusion_cowork.nodes.ai",
    "AISummarizeNode": "fusion_cowork.nodes.ai",
    "AIGenerateNameNode": "fusion_cowork.nodes.ai",
    # IO nodes
    "FileInputNode": "fusion_cowork.nodes.io",
    "FileOutputNode": "fusion_cowork.nodes.io",
    # Logic nodes
    "FilterNode": "fusion_cowork.nodes.logic",
    "LoopNode": "fusion_cowork.nodes.logic",
    "MergeNode": "fusion_cowork.nodes.logic",
    # Tool nodes (new)
    "ShellExecNode": "fusion_cowork.nodes.tools",
    "PythonREPLNode": "fusion_cowork.nodes.tools",
    "PythonREPLCodeExecutor": "fusion_cowork.nodes.tools",
    "WebSearchNode": "fusion_cowork.nodes.tools",
    "FetchURLNode": "fusion_cowork.nodes.tools",
    "ApplyEditNode": "fusion_cowork.nodes.tools",
    # Browser nodes (new)
    "BrowserOpenNode": "fusion_cowork.nodes.browser",
    "BrowserExtractNode": "fusion_cowork.nodes.browser",
    "BrowserAutomateNode": "fusion_cowork.nodes.browser",
    "BrowserClient": "fusion_cowork.nodes.browser",
    "BrowserManager": "fusion_cowork.nodes.browser",
    # V0.2 enhanced scheduling
    "EnhancedScheduler": "fusion_cowork.engine.enhanced_scheduler",
    "TaskExecution": "fusion_cowork.engine.enhanced_scheduler",
    "TaskDependency": "fusion_cowork.engine.enhanced_scheduler",
    # V0.2 AI optimization
    "WorkflowOptimizer": "fusion_cowork.engine.optimizer",
    "OptimizationSuggestion": "fusion_cowork.engine.optimizer",
    "WorkflowAnalysis": "fusion_cowork.engine.optimizer",
    # V0.2 report generation
    "ReportGenerator": "fusion_cowork.report.report_generator",
    "ReportConfig": "fusion_cowork.report.report_generator",
    # V0.3 multi-agent
    "AgentOrchestrator": "fusion_cowork.orchestrator.orchestrator",
    "Agent": "fusion_cowork.orchestrator.orchestrator",
    "AgentRole": "fusion_cowork.orchestrator.orchestrator",
    "AgentTask": "fusion_cowork.orchestrator.orchestrator",
    # V0.3 cross-device collaboration
    "CrossDeviceSync": "fusion_cowork.server.sync",
    "Device": "fusion_cowork.server.sync",
    "SyncMessage": "fusion_cowork.server.sync",
    # Claude Cowork parity nodes
    "ScreenCaptureNode": "fusion_cowork.nodes.macos",
    "ClipboardNode": "fusion_cowork.nodes.macos",
    "NotificationNode": "fusion_cowork.nodes.macos",
    "AppLifecycleNode": "fusion_cowork.nodes.macos",
    "OCRNode": "fusion_cowork.nodes.macos",
    # MCP Server
    "MCPServer": "fusion_cowork.server.mcp_server",
    # AI client
    "FusionMLXClient": "fusion_cowork.ai",
    "KBClient": "fusion_cowork.ai",
    "NLWorkflowGenerator": "fusion_cowork.ai",
    # Templates
    "TemplateManager": "fusion_cowork.templates",
    # M3 plugin system
    "PluginManifest": "fusion_cowork.plugins.manifest",
    "PluginLoader": "fusion_cowork.plugins.loader",
    "SandboxedNode": "fusion_cowork.plugins.loader",
    "make_sandboxed_node_class": "fusion_cowork.plugins.loader",
    # M3 skill mechanism
    "Skill": "fusion_cowork.skills.registry",
    "SkillRegistry": "fusion_cowork.skills.registry",
    "register_builtin_skills": "fusion_cowork.skills.builtin",
    "BUILTIN_SKILLS": "fusion_cowork.skills.builtin",
    # M3 Chrome CDP
    "CDPClient": "fusion_cowork.nodes.browser",
    "CDPNavigateNode": "fusion_cowork.nodes.browser",
    "CDPSnapshotNode": "fusion_cowork.nodes.browser",
    "CDPClickNode": "fusion_cowork.nodes.browser",
    "CDPFillNode": "fusion_cowork.nodes.browser",
    "CDPFillFormNode": "fusion_cowork.nodes.browser",
    "CDPScreenshotNode": "fusion_cowork.nodes.browser",
    "CDPEvaluateNode": "fusion_cowork.nodes.browser",
    "CDPEmulateNode": "fusion_cowork.nodes.browser",
    "CDPNetworkNode": "fusion_cowork.nodes.browser",
    "CDPConsoleNode": "fusion_cowork.nodes.browser",
    # M4 Computer Use nodes
    "MouseMoveNode": "fusion_cowork.nodes.macos",
    "MouseClickNode": "fusion_cowork.nodes.macos",
    "KeyboardTypeNode": "fusion_cowork.nodes.macos",
    "KeyboardShortcutNode": "fusion_cowork.nodes.macos",
    "ComputerUseLoopNode": "fusion_cowork.nodes.macos",
    # M4 remote control
    "RemoteControlServer": "fusion_cowork.server.remote",
    "RemoteControlClient": "fusion_cowork.server.remote",
    # M4 structured output
    "OutputSchema": "fusion_cowork.engine.schema",
    # M5 Agent Runtime
    "AgentRuntime": "fusion_cowork.orchestrator.agent_runtime",
    "CoordinatorExecutor": "fusion_cowork.orchestrator.executors",
    # issue #190 team message durable mailbox + resident consumer pool
    "TeamMailbox": "fusion_cowork.orchestrator.team_mailbox",
    "CoworkRoster": "fusion_cowork.orchestrator.team_mailbox",
    "RunnerPool": "fusion_cowork.orchestrator.runner_pool",
    # M5 SDK/Headless
    "FusionCoworkSDK": "fusion_cowork.sdk",
    "HeadlessRunner": "fusion_cowork.sdk.headless",
    # M6 collaboration space
    "Space": "fusion_cowork.space",
    "SpaceConfig": "fusion_cowork.space",
    "SpaceMember": "fusion_cowork.space",
    "SpaceMessage": "fusion_cowork.space",
    "SpaceSnapshot": "fusion_cowork.space",
    "PeerInfo": "fusion_cowork.space",
    "SpaceRole": "fusion_cowork.space",
    "SpaceStatus": "fusion_cowork.space",
    "SpaceStore": "fusion_cowork.space",
    "SpaceService": "fusion_cowork.space",
    "SpaceMemberService": "fusion_cowork.space",
    "SpacePermission": "fusion_cowork.space",
    # M7 shared chat + knowledge binding
    "SpaceChatService": "fusion_cowork.space",
    "SpaceKBService": "fusion_cowork.space",
    "create_space_api": "fusion_cowork.space",
    "SharedContext": "fusion_cowork.space",
    "inject_shared_context": "fusion_cowork.space",
    "extract_shared_context": "fusion_cowork.space",
    "SpaceAgentRuntime": "fusion_cowork.space",
    "AgentStudioClient": "fusion_cowork.space",
    # FD-05 ConfigCenter
    "ConfigCenter": "fusion_cowork.config_center",
    "ConfigChange": "fusion_cowork.config_center",
    "ConfigObserver": "fusion_cowork.config_center",
    # FD-06 MCP Gateway
    "MCPGateway": "fusion_cowork.server.mcp_gateway",
    "ManagedProcess": "fusion_cowork.server.mcp_gateway",
    "ProcessStatus": "fusion_cowork.server.mcp_gateway",
    "TransportType": "fusion_cowork.server.mcp_gateway",
    # FD-07 Plugin Sandbox
    "PluginSandbox": "fusion_cowork.plugins.sandbox",
    "ResourceLimits": "fusion_cowork.plugins.sandbox",
    "SandboxResult": "fusion_cowork.plugins.sandbox",
    "SandboxStatus": "fusion_cowork.plugins.sandbox",
    # D1 trajectory flywheel
    "TrajectoryRecorder": "fusion_cowork.trajectory",
    "TrajectoryWriter": "fusion_cowork.trajectory",
    "SpaceTrajectoryExporter": "fusion_cowork.trajectory",
    "SpaceMessageTrajectory": "fusion_cowork.trajectory",
    # V0.2.13 mobile push notification
    "PushConfig": "fusion_cowork.notification",
    "PushResult": "fusion_cowork.notification",
    "push": "fusion_cowork.notification",
    "resolve_config": "fusion_cowork.notification",
    # V0.2.13 Deep Research
    "ResearchSubQuestion": "fusion_cowork.research",
    "ResearchFinding": "fusion_cowork.research",
    "ResearchReport": "fusion_cowork.research",
    "run_deep_research": "fusion_cowork.research",
    # V0.2.13 UltraReview
    "ReviewFinding": "fusion_cowork.review",
    "ReviewReport": "fusion_cowork.review",
    "LENS_DEFINITIONS": "fusion_cowork.review",
    "run_ultra_review": "fusion_cowork.review",
    # V0.2.14 interactive agent loop
    "AgentLoop": "fusion_cowork.agent_loop",
    "AgentAction": "fusion_cowork.agent_loop",
    "AgentTurn": "fusion_cowork.agent_loop",
    "AgentLoopResult": "fusion_cowork.agent_loop",
    "run_agent_loop": "fusion_cowork.agent_loop",
    # V0.2.14 real-time presence + cursor
    "PresenceManager": "fusion_cowork.space",
    "PresenceState": "fusion_cowork.space",
    # V0.2.14 collaboration-layer bidirectional WS
    "CollabHub": "fusion_cowork.server.collab_ws",
    # V0.4.0 multi-tenant cloud — tenant/auth/persistence
    "TenantPrincipal": "fusion_cowork.tenant",
    "get_current_tenant": "fusion_cowork.tenant",
    "set_current_tenant": "fusion_cowork.tenant",
    "tenant_context": "fusion_cowork.tenant",
    "JWTVerifier": "fusion_cowork.auth",
    # V0.5.3 fusion-identity integration (issue #88, unified JWT issuance + tenant registry)
    "IdentityClient": "fusion_cowork.auth.identity",
    "IdentityVerifyResult": "fusion_cowork.auth.identity",
    "is_identity_enabled": "fusion_cowork.auth.identity",
    "get_identity_client": "fusion_cowork.auth.identity",
    "make_verify_jwt_callback": "fusion_cowork.auth.identity",
    "reset_identity_client": "fusion_cowork.auth.identity",
    "MigrationRunner": "fusion_cowork.db",
    "Migration": "fusion_cowork.db",
    "BackupManager": "fusion_cowork.db",
    # V0.4.0 observability
    "trace_context": "fusion_cowork.observability",
    "get_trace_id": "fusion_cowork.observability",
    "new_trace_id": "fusion_cowork.observability",
    "HealthCheck": "fusion_cowork.observability",
    "get_meter": "fusion_cowork.observability.metrics",
    "get_tracer": "fusion_cowork.observability.tracing",
    # V0.4.0 security at scale
    "RateLimiter": "fusion_cowork.security",
    "TokenBucket": "fusion_cowork.security",
    "encrypt_at_rest": "fusion_cowork.security",
    "decrypt_at_rest": "fusion_cowork.security",
    "AuditLog": "fusion_cowork.security",
    "CircuitBreaker": "fusion_cowork.security",
    "CircuitOpenError": "fusion_cowork.security",
    "TenantQuotas": "fusion_cowork.security",
    "QuotaEnforcer": "fusion_cowork.security",
    "QuotaExceededError": "fusion_cowork.security",
    # V0.4.5 fusion-guard integration (issue #73, HIGH_RISK_NODES -> guard.evaluate)
    "GuardClient": "fusion_cowork.security.guard",
    "GuardVerdict": "fusion_cowork.security.guard",
    "guard_enabled": "fusion_cowork.security.guard",
    "get_guard_client": "fusion_cowork.security.guard",
    "close_guard_client": "fusion_cowork.security.guard",
    "node_to_guard_content": "fusion_cowork.security.guard",
    # V0.4.0 plugin signing/registry
    "PluginRegistry": "fusion_cowork.plugins.registry",
    "sign_manifest": "fusion_cowork.plugins.signing",
    "verify_manifest": "fusion_cowork.plugins.signing",
    "verify_any_key": "fusion_cowork.plugins.signing",
    # V0.4.0 cluster sync (issue #61, migrated from fusion-multi-node)
    "ClusterSyncManager": "fusion_cowork.cluster_sync",
    "ModelManifest": "fusion_cowork.cluster_sync",
    "FileEntry": "fusion_cowork.cluster_sync",
    "NodeLoadReport": "fusion_cowork.cluster_sync",
    "PartitionDetector": "fusion_cowork.cluster_sync",
    "PartitionState": "fusion_cowork.cluster_sync",
    "NodeHealth": "fusion_cowork.cluster_sync",
    "build_manifest": "fusion_cowork.cluster_sync",
    "compute_sync_diff": "fusion_cowork.cluster_sync",
    "compute_file_sha256": "fusion_cowork.cluster_sync",
    "is_safe_peer_host": "fusion_cowork.cluster_sync",
    "is_safe_path_segment": "fusion_cowork.cluster_sync",
    "build_safe_url": "fusion_cowork.cluster_sync",
    # V0.5.0 distributed state layer (issue #79, cluster-aware handle wrapping)
    "DistributedStateStore": "fusion_cowork.distributed_state",
    "ClusterState": "fusion_cowork.distributed_state",
    "ClusterNode": "fusion_cowork.distributed_state",
    "VramAllocation": "fusion_cowork.distributed_state",
    "PluginState": "fusion_cowork.distributed_state",
    "ClusterNodeRegistry": "fusion_cowork.distributed_state",
    "ClusterTaskScheduler": "fusion_cowork.distributed_state",
    "is_cluster_enabled": "fusion_cowork.distributed_state",
    "resolve_node_id": "fusion_cowork.distributed_state",
    "resolve_state_path": "fusion_cowork.distributed_state",
    "get_cluster_state_store": "fusion_cowork.distributed_state",
    "reset_cluster_state_store": "fusion_cowork.distributed_state",
}

_lazy_cache: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazily loaded module (adopted from the Squish __getattr__ pattern).

    仅在首次访问时导入对应模块，之后缓存结果。  # i18n: user-facing
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
                f"module 'fusion_cowork' has no attribute {name!r} (lazy import {mod_name!r} failed: {exc})"
            ) from None
    raise AttributeError(f"module 'fusion_cowork' has no attribute {name!r}")


def __dir__() -> list[str]:
    """List all accessible attributes."""
    return sorted(set([*dir(type("", (), {})), *__all__, *_LAZY_IMPORTS.keys()]))


__all__ = [
    "__version__",
    "__app_name__",
    "NODE_NAME_ALIASES",
    *_LAZY_IMPORTS.keys(),
]
