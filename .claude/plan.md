# Fusion-Desk API → Fusion-Studio GUI 全量对齐方案

## 现状分析

### fusion-desk 后端功能域 (25个功能点)

| # | 功能域 | 后端API | CLI命令 | GUI现状 |
|---|--------|---------|---------|---------|
| 1 | 工作流引擎 | WorkflowEngine execute/cancel | workflow run/list | ❌ 无 |
| 2 | 工作流DAG | Workflow构建/序列化 | 无 | ❌ 无(DAGCanvasView是通用) |
| 3 | 节点系统 | 6类40+节点 | 间接通过workflow | ❌ 无 |
| 4 | 模板系统 | TemplateManager list/get/run | template list/show/run | ⚠️ 模拟数据 |
| 5 | 调度器 | TaskScheduler add/list/remove/start/stop | schedule * | ❌ 无 |
| 6 | AI生成 | NLWorkflowGenerator generate | ai generate | ❌ 无 |
| 7 | AI服务 | FusionMLXClient status | ai status | ⚠️ IPCClient有mlx方法 |
| 8 | 智能体编排 | AgentOrchestrator submit/list/status | 无 | ❌ 无 |
| 9 | 智能体运行时 | AgentRuntime start/stop/submit | 无 | ❌ 无 |
| 10 | Hook系统 | HookManager register/unregister/fire | 无 | ❌ 无 |
| 11 | 权限系统 | PermissionManager check/approve/deny/list | permission * | ❌ 无 |
| 12 | 会话管理 | SessionStore list/get/fork/delete | session * | ❌ 无 |
| 13 | MCP服务器 | MCPServer 15个工具 | mcp | ❌ 无 |
| 14 | 嵌入浏览器 | BrowserManager build/start | browser * | ❌ 无 |
| 15 | Computer Use | screenshot→analyze→act | computer-use * | ❌ 无 |
| 16 | 远程控制 | CrossDeviceSync WebSocket | remote * | ❌ 无 |
| 17 | 报告生成 | ReportGenerator generate | 无 | ❌ 无 |
| 18 | 基准测试 | BenchmarkRunner run/report | benchmark * | ❌ 无 |
| 19 | SDK | FusionDeskSDK | 无 | ❌ 无 |
| 20 | DeskRPC | 25个desk.*方法 | desk | ⚠️ 后端有,IPC缺 |
| 21 | 技能系统 | SkillManager list/execute | skill * | ❌ 无 |
| 22 | 插件系统 | PluginManager discover/install | plugin * | ❌ 无 |
| 23 | 结构化输出 | OutputSchema validate/check | schema * | ❌ 无 |
| 24 | 系统工具 | SystemInfo/DiskCleaner | system * | ❌ 无 |

### 关键问题

1. **DeskView 完全模拟** — 0个IPC调用,硬编码6个模板,Task.sleep模拟执行
2. **IPCClient缺desk.*方法** — 没有1个desk.*方法,无法连接后端
3. **25个功能域0个GUI** — 仅模板有模拟界面,其余全部缺失

## 实施方案

### 第1步: IPC Bridge 补齐 (IPCClient.swift)

在IPCClient.swift添加 `// MARK: - Desk` 方法族，对齐DeskRPCServer 25个方法 + 18个新增方法。

### 第2步: DeskRPCServer 扩展 (desk_rpc.py)

补充18个缺失RPC方法: schedule.* / ai.* / hook.* / template.* / skill.* / plugin.* / report.* / benchmark.* / browser.*

### 第3步: DeskView 重构为8-Tab架构

```
DeskView (主视图,8个Tab)
├── Tab 1: 模板中心 (DeskTemplateCenterView) — 连接desk.template.*
├── Tab 2: 工作流 (DeskWorkflowView) — 连接desk.workflow.* / desk.nodes.*
├── Tab 3: 调度 (DeskScheduleView) — 连接desk.schedule.*
├── Tab 4: 智能体 (DeskAgentView) — 连接desk.agent.* + runtime状态
├── Tab 5: 会话 (DeskSessionView) — 连接desk.session.*
├── Tab 6: 权限 (DeskPermissionView) — 连接desk.permission.*
├── Tab 7: 系统 (DeskSystemView) — 连接desk.system.* / desk.mlx.* / desk.browser.*
└── Tab 8: 工具 (DeskToolsView) — skill / plugin / benchmark / report / MCP
```

### 文件清单

**fusion-studio 新建/修改:**
| 文件 | 操作 |
|------|------|
| Bridge/IPCClient.swift | 修改: 添加43个desk.*方法 |
| Modules/Desk/DeskView.swift | 重写: 8-Tab主视图 |
| Modules/Desk/DeskModels.swift | 新建: 共享数据模型 |
| Modules/Desk/DeskTemplateCenterView.swift | 新建: 模板中心 |
| Modules/Desk/DeskWorkflowView.swift | 新建: 工作流管理 |
| Modules/Desk/DeskScheduleView.swift | 新建: 调度管理 |
| Modules/Desk/DeskAgentView.swift | 新建: 智能体管理 |
| Modules/Desk/DeskSessionView.swift | 新建: 会话管理 |
| Modules/Desk/DeskPermissionView.swift | 新建: 权限管理 |
| Modules/Desk/DeskSystemView.swift | 新建: 系统面板 |
| Modules/Desk/DeskToolsView.swift | 新建: 工具面板 |

**fusion-desk 修改:**
| 文件 | 操作 |
|------|------|
| fusion_desk/server/desk_rpc.py | 修改: 添加18个新RPC方法 |

### 实施顺序

1. **P0**: IPCClient desk.*方法 + DeskRPCServer扩展 + DeskModels + DeskView主框架
2. **P0**: 模板中心 + 工作流 + 智能体 (核心3个Tab)
3. **P1**: 调度 + 会话 + 权限 + 系统 (管理4个Tab)
4. **P2**: 工具面板 (补充Tab)
5. 验证: 启动fusion-desk desk服务 → fusion-studio连接 → 各Tab功能测试
