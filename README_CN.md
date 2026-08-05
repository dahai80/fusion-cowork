[English](README.md) | [中文](README_CN.md)

---

<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>macOS 原生、本地优先、零代码桌面智能自动化平台</strong></p>
  <p><em>让 Mac 自己干活，本地 AI 全自动桌面办公</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="许可证">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/离线优先-核心特性-important" alt="离线优先">
  <img src="https://img.shields.io/badge/状态-beta-yellow" alt="Beta">
  <img src="https://github.com/dahai80/fusion-cowork/actions/workflows/ci.yml/badge.svg" alt="CI">
</p>

---

## 📋 产品简介

**Fusion-Cowork** 是 [Fusion-MLX](https://github.com/fusion-mlx) 全栈 Apple Silicon 本地 AI 生态的三大旗舰核心产品之一，面向所有办公用户提供零代码桌面智能自动化能力。

### 生态层级定位

| 产品 | 定位 | 面向用户 |
|------|------|----------|
| **Fusion-Code** | 开发者编码智能体 | 程序员 |
| **Fusion-Agent-Studio** | 高级智能体工作流编排 | 开发者 / 架构师 |
| **Fusion-Cowork** 🎯 | 大众用户成品自动化工具 | **所有办公用户** |

> Studio 造流程、Desk 用流程、Code 写能力、KB 存知识、Hub 管模型

### 核心差异化优势

- ✅ **100% 本地离线** — 零文件上传、零隐私泄露、无埋点
- ✅ **macOS 原生深度适配** — 桌面、下载、文稿目录专项自动化
- ✅ **MLX 硬件加速** — Apple Silicon 原生推理，无需 GPU
- ✅ **AI 语义理解** — 理解文件内容，不只是文件名
- ✅ **零代码操作** — 一句话描述需求，自动生成完整流程
- ✅ **Fusion 全生态互通** — 与 Code / Studio / KB / Hub 无缝联动

---

## 🚀 快速开始

### 安装

```bash
# 克隆项目
git clone https://github.com/dahai80/fusion-cowork.git
cd fusion-cowork

# 安装依赖
pip install -e .

# 安装 AI 支持（可选，建议）
pip install -e ".[web]"

# 或使用 uv（更快）
uv venv --python 3.11
uv pip install -e ".[test]"
```

### 基础使用

```bash
# 查看可用模板
fusion-cowork template list

# 查看模板详情
fusion-cowork template show desktop_daily_cleanup

# 运行模板（预览模式）
fusion-cowork template run desktop_daily_cleanup --dry-run

# 运行模板（实际执行）
fusion-cowork template run desktop_daily_cleanup

# AI 一句话生成工作流
fusion-cowork ai generate "帮我把桌面所有 PDF 按主题分类归档"

# 检查 AI 服务状态
fusion-cowork ai status

# 查看系统信息
fusion-cowork system info

# 启动 MCP 服务 (stdio 模式，供 Claude Code 调用)
fusion-cowork mcp serve

# 启动 MCP 服务 (HTTP 模式)
fusion-cowork mcp serve --transport http --port 11438

# 启动 Desk RPC 服务 (供 Fusion-Studio GUI 调用)
fusion-cowork desk rpc

# 生命周期管理 (start.sh)
./start.sh start    # 启动 desk RPC 守护进程
./start.sh stop     # 优雅停止
./start.sh restart  # 停止 + 启动
./start.sh status   # PID、Socket、内存、运行时间
./start.sh log [-f] # 查看日志 (-f 实时跟踪)
./start.sh doctor   # 健康检查 (venv、CLI、Socket、上游服务)
./start.sh clean    # 轮转日志、清理 __pycache__
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FUSION_MLX_API_KEY` | `local` | fusion-mlx API 密钥（需匹配 `~/.fusion-mlx/settings.json` 中 `auth.api_key`） |
| `FUSION_MLX_URL` | `http://localhost:11434/v1` | fusion-mlx 基础 URL |
| `FUSION_RAG_URL` | `http://localhost:11436` | fusion-rag (fusion-kb) 基础 URL |

```bash
# 示例：设置 API 密钥
export FUSION_MLX_API_KEY="your-api-key-here"
fusion-cowork desk rpc

# 会话管理
fusion-cowork session list
fusion-cowork session show <session_id>
fusion-cowork session fork <session_id> --from-step 2
fusion-cowork session cleanup --days 30

# 权限管理
fusion-cowork permission level <manual|auto|plan|bypass>
fusion-cowork permission approve <tool_name> --scope <scope>
fusion-cowork permission deny <tool_name> --scope <scope>
fusion-cowork permission list

# 功能对比基准
fusion-cowork benchmark report --format markdown|html|json [-o report.md]
fusion-cowork benchmark run --node file_input --node shell_exec --repeats 3

# 插件管理 (M3)
fusion-cowork plugin list
fusion-cowork plugin install /path/to/plugin
fusion-cowork plugin load <name>
fusion-cowork plugin uninstall <name>

# 技能管理 (M3)
fusion-cowork skill list
fusion-cowork skill run /cleanup
fusion-cowork skill search "clean"

# Chrome CDP — 远程浏览器控制 (M3)
fusion-cowork cdp navigate https://example.com
fusion-cowork cdp snapshot
fusion-cowork cdp click 42
fusion-cowork cdp fill --selector "#search" --value "hello"
fusion-cowork cdp screenshot --save ~/Desktop/shot.png
fusion-cowork cdp evaluate "document.title"

# 协作空间 (M6+M7)
fusion-cowork space create --name "Project Alpha" --owner-id user1
fusion-cowork space list
fusion-cowork space get <space_id>
fusion-cowork space archive <space_id>
fusion-cowork space member list <space_id>
fusion-cowork space member invite <space_id> --inviter-id user1 --role member
fusion-cowork space member join <invite_code> --user-id user2
fusion-cowork space member remove <space_id> --user-id user3 --operator-id user1
fusion-cowork space chat <space_id> --user user1 --agent agent1
fusion-cowork space knowledge bind <space_id> --operator user1
fusion-cowork space knowledge status <space_id>
fusion-cowork space knowledge upload <space_id> doc.pdf --operator user1
fusion-cowork space knowledge search <space_id> "query text" --top-k 5
fusion-cowork space knowledge unbind <space_id> --operator user1
```

---

## 🎯 内置模板（10 个开箱即用）

| 模板 | 分类 | 说明 | 需要 AI |
|------|------|------|----------|
| 🧹 **桌面每日规整** | 桌面清理 | 按类型自动规整桌面文件 | ❌ |
| 📥 **下载文件夹自动归档** | 文件整理 | 按类型归档、去重、整理下载文件夹 | ❌ |
| 📝 **文档批量 AI 摘要** | AI 处理 | 批量总结文档生成汇总报告 | ✅ |
| 🏷️ **AI 智能文件分类归档** | AI 处理 | AI 根据内容语义自动分类归档 | ✅ |
| ✏️ **AI 批量智能重命名** | AI 处理 | AI 根据内容生成规范文件名 | ✅ |
| 💾 **磁盘空间清理** | 系统维护 | 扫描清理缓存和垃圾文件 | ❌ |
| 📂 **项目资料一键归集** | 文件整理 | 按类型归集项目资料 | ❌ |
| 🔍 **重复文件扫描清理** | 系统维护 | 扫描并清理重复文件 | ❌ |
| 📋 **日志文件自动清洗** | 系统维护 | 扫描清理日志文件 | ❌ |
| 🖼️ **图片批量重命名分类** | 文件整理 | 批量重命名图片并分类 | ❌ |

### 行业模板 (V0.3)

| 模板 | 行业 | 说明 |
|------|------|------|
| 🎨 **设计资产整理** | 设计 | 自动分类 PSD/AI/Sketch 文件 |
| 🖼️ **设计批量导出** | 设计 | 批量导出设计资产 |
| 💻 **项目代码清理** | 开发 | 清理缓存、pycache、node_modules |
| 🔍 **日志分析器** | 开发 | AI 驱动日志分析 |
| 🚀 **项目脚手架** | 开发 | 自动创建项目结构 |
| 📊 **CSV 数据清洗** | 数据 | 去重、规范化 CSV |
| 🔄 **格式转换器** | 数据 | 批量 CSV↔JSON↔Excel |
| 🖥️ **系统巡检** | 运维 | 收集系统健康报告 |
| 💾 **备份管理器** | 运维 | 定时文件备份 |
| ⚠️ **磁盘告警** | 运维 | 监控磁盘用量、自动清理 |

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────┐
│                     UI 层                              │
│   CLI (click)  │  Web UI (FastAPI)  │  macOS (.app)   │
├─────────────────────────────────────────────────────┤
│                  流程引擎层                             │
│   WorkflowEngine  │  TaskScheduler  │  NodeRegistry   │
│   (DAG, n8n 启发)  │  (APScheduler)  │  (28 个节点)    │
├─────────────────────────────────────────────────────┤
│                   AI 能力层                             │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (自然语言 → 工作流)          │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 系统能力层                              │
│   macOS 节点 (AppleScript / osascript)                │
│   文件操作  │  桌面整理  │  磁盘清理  │  通用工具       │
├─────────────────────────────────────────────────────┤
│              协作空间层 🆕                              │
│   SpaceStore (SQLite WAL)  │  SpaceService (CRUD)     │
│   SpacePermission (4级)     │  SpaceMemberService     │
│   SpaceChatService (SSE)   │  SpaceKBService (RAG)    │
│   SharedContext (节点)      │  SpaceAPI (REST+SSE)     │
└─────────────────────────────────────────────────────┘
```

### 节点类型（33 个内置节点）

| 分类 | 数量 | 节点 |
|------|------|------|
| `macos_system` | 15 | 桌面清理、下载整理、磁盘清理、文件监听、文件分类、批量重命名、复制、移动、删除、查找、**鼠标移动、鼠标点击、键盘输入、键盘快捷键、Computer Use 循环** 🆕 |
| `ai_processing` | 4 | AI 分类、AI 摘要、AI 重命名、**OCR** 🆕 |
| `tool` | 5 | Shell 命令、Python REPL、Web 搜索、获取网页、文件编辑 |
| `browser` | 3 | 浏览器打开、浏览器提取、浏览器自动化 |
| `cdp` | 10 | CDP 导航、快照、点击、填写、批量填写、截图、执行JS、模拟设备、网络、控制台 |
| `io` | 2 | 文件输入、文件输出 |
| `logic` | 3 | 条件过滤、循环处理、数据合并 |
| **Claude Cowork 对等** | **6** | **屏幕截图、剪贴板、通知、应用生命周期、OCR、MCP 服务** |

### Claude Cowork 对等能力 (V0.2) 🆕

| 能力 | 状态 | 实现 |
|------|------|------|
| 屏幕截图与桌面视图 | ✅ | `ScreenCaptureNode` — 全屏/选区/窗口截图 |
| 剪贴板读写 | ✅ | `ClipboardNode` — pbpaste/pbcopy |
| 系统通知 | ✅ | `NotificationNode` — macOS 通知中心 |
| macOS 应用生命周期 | ✅ | `AppLifecycleNode` — 启动/退出/激活/列表 |
| OCR / 屏幕文字识别 | ✅ | `OCRNode` — Vision + fusion-mlx |
| MCP 协议服务 | ✅ | `MCPServer` — 15 个工具供 Claude Desktop/Code 调用 |

### 生态互通

| 组件 | 协议 | 用途 |
|------|------|------|
| **fusion-mlx** | HTTP API (端口 8000) | LLM 推理、文本生成、嵌入 |
| **Fusion-KB** | HTTP API (端口 11434) | 知识库语义检索、RAG |
| **Fusion-Code** | 自动生成脚本 | 复杂逻辑自动执行 |
| **Agent-Studio** | 工作流导入 | 调用高级编排的工作流 |
| **Model-Hub** | 模型调度 | 自动选择最优本地模型 |

---

## 🔧 节点参考

### macOS 系统节点

| 节点 | 功能 |
|------|------|
| `desktop_clean` | 桌面按类型/日期规整 |
| `download_organizer` | 下载文件夹归档去重 |
| `disk_cleaner` | 磁盘缓存垃圾清理 |
| `file_watcher` | 目录变化监听 |
| `screen_capture` 🆕 | 桌面截图（全屏/选区/窗口） |
| `clipboard` 🆕 | 系统剪贴板读写 |
| `notification` 🆕 | macOS 通知中心推送 |
| `app_lifecycle` 🆕 | macOS 应用启动/退出/激活/列表 |
| `ocr` 🆕 | 图片文字识别（Vision + MLX） |

### AI 处理节点（→ 调用 fusion-mlx）

| 节点 | 功能 | 后端 |
|------|------|------|
| `ai_classify` | 语义文件分类 | `fusion-mlx /v1/chat/completions` |
| `ai_summarize` | 文档摘要生成 | `fusion-mlx /v1/chat/completions` |
| `ai_generate_name` | 智能文件命名 | `fusion-mlx /v1/chat/completions` |

### 工具节点（吸纳自 Squish 内置工具集）

| 节点 | 功能 |
|------|------|
| `shell_exec` | 执行 Shell 命令，捕获输出 |
| `python_repl` | 在隔离子进程执行 Python 代码 |
| `web_search` | 通过 DuckDuckGo Lite 搜索网页 |
| `fetch_url` | 获取 URL 内容，自动提取文本 |
| `apply_edit` | 对文件做查找替换编辑 |

### IO / 逻辑节点

| 节点 | 功能 |
|------|------|
| `file_input` | 读取文件列表或目录内容 |
| `file_output` | 将工作流结果写入文件 |
| `filter` | 按扩展名/大小/日期过滤 |
| `loop` | 批量处理列表中的每个元素 |
| `merge` | 合并多个上游数据源 |

### Chrome CDP 节点 🆕 (M3)

| 节点 | 功能 |
|------|------|
| `cdp_navigate` | 通过 CDP 导航到 URL |
| `cdp_snapshot` | 获取页面无障碍树 |
| `cdp_click` | 按 backendNodeId 点击元素 |
| `cdp_fill` | 按 CSS 选择器填写表单 |
| `cdp_fill_form` | 批量填写多个表单字段 |
| `cdp_screenshot` | 截取页面截图 (PNG) |
| `cdp_evaluate` | 在页面中执行 JavaScript |
| `cdp_emulate` | 模拟设备视口 |
| `cdp_network` | 查询网络请求 |
| `cdp_console` | 查询控制台消息 |

### 插件系统 🆕 (M3)

插件系统支持用自定义节点扩展 Fusion-Cowork：

```python
# 插件清单 (manifest.json)
{
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "自定义节点",
    "nodes": ["my_custom_node"],
    "entry_point": "plugin",
}
```

```bash
# 从目录或 zip 安装
fusion-cowork plugin install /path/to/plugin_dir
fusion-cowork plugin install /path/to/plugin.zip
```

### 技能机制 🆕 (M3)

技能是映射到现有节点的高级快捷方式：

| 技能 | 别名 | 节点 |
|------|------|------|
| `/cleanup` | 清理桌面 | `desktop_clean` |
| `/classify` | AI分类 | `ai_classify` |
| `/screenshot` | 截图 | `screen_capture` |
| `/search` | 搜索 | `web_search` |
| `/organize` | 下载整理 | `download_organizer` |
| `/diskclean` | 磁盘清理 | `disk_cleaner` |

MCP 工具 `skill_list` 和 `skill_run` 可供 Claude Desktop/Code 集成调用。

### Computer Use 节点 🆕 (M4)

鼠标/键盘控制 + AI 循环，实现自主桌面操作：

| 节点 | 功能 |
|------|------|
| `mouse_move` | 移动鼠标到 (x, y) 坐标 |
| `mouse_click` | 点击（左/右/双击） |
| `keyboard_type` | 键盘输入文本 |
| `keyboard_shortcut` | 按组合键（Cmd+C 等） |
| `computer_use_loop` | 截图 → AI 分析 → 操作 → 重复循环 |

```bash
# 移动鼠标
fusion-cowork computer-use move 500 300

# 点击
fusion-cowork computer-use click --x 500 --y 300 --button left

# 输入文本
fusion-cowork computer-use type "Hello World"

# 键盘快捷键
fusion-cowork computer-use shortcut c --modifiers cmd

# AI 驱动 Computer Use 循环
fusion-cowork computer-use run "打开 Safari 并搜索天气" --max-steps 10
```

### 远程控制 🆕 (M4)

基于 WebSocket 的远程控制，支持外部客户端：

```bash
# 启动远程服务
fusion-cowork remote serve --port 11439 --token mytoken

# 从另一台机器连接
fusion-cowork remote connect ws://host:11439/control --token mytoken

# 远程提交工作流
fusion-cowork remote submit workflow.json --url ws://host:11439/control
```

### 结构化输出 🆕 (M4)

JSON Schema 验证节点输出：

```bash
# 验证数据是否符合 schema
fusion-cowork schema validate data.json schema.json

# 检查节点输出 schema
fusion-cowork schema check mouse_move
```

NodeResult 支持 `schema` 字段和 `validate()` 方法，自动进行输出验证。

---

## 📝 工作流示例

### 桌面每日规整 (JSON)

```json
{
  "name": "桌面每日规整",
  "nodes": [
    {
      "id": "n1",
      "name": "file_input",
      "config": { "params": { "path": "~/Desktop" } }
    },
    {
      "id": "n2",
      "name": "desktop_clean",
      "config": { "params": { "organize_by_type": true } }
    }
  ],
  "edges": [
    { "source_id": "n1", "target_id": "n2" }
  ]
}
```

### AI 一句话生成

```bash
fusion-cowork ai generate "每天晚上9点自动清理下载文件夹并备份到文稿"
```

---

## 🧪 运行测试

```bash
# 安装测试依赖
pip install -e ".[test]"

# 运行所有测试
pytest tests/ -v

# 生成覆盖率报告
pytest tests/ --cov=fusion_cowork --cov-report=html
```

---

## 🛣️ 迭代路线

### V0.1 (MVP) ✅
- [x] 基础文件自动化（桌面/下载/文档整理）
- [x] AI 批量处理（分类/摘要/重命名）
- [x] 模板中心（10 个内置模板）
- [x] 自然语言生成工作流
- [x] 生态基础打通（fusion-mlx / Fusion-KB）
- [x] 参数类型强制转换（吸纳自 Squish）
- [x] Lazy Import 架构（吸纳自 Squish）
- [x] 工具节点：Shell、Python REPL、Web 搜索、获取网页、文件编辑

### V0.2 ✅
- [x] 增强调度器（Cron UI、日历视图、任务依赖、统计）
- [x] AI 工作流优化器（瓶颈检测、自动修复、fusion-mlx 建议）
- [x] 批量报表生成（Markdown/HTML、多工作流汇总）
- [x] macOS 原生 .app 打包脚本
- [x] 屏幕截图（桌面截图 + AI 分析）
- [x] 剪贴板集成（读写清空）
- [x] 系统通知（macOS 通知中心）
- [x] macOS 应用生命周期（启动/退出/激活/列表）
- [x] OCR 文字识别（Vision + fusion-mlx）
- [x] MCP 服务模式（15 个工具供 Claude Desktop/Code 调用）

### V0.3 ✅
- [x] 行业自动化模板（设计/开发/数据/运维 — 10 个模板）
- [x] 多智能体联动（注册、任务分解、并行执行）
- [x] 跨设备协同（WebSocket 同步、设备发现、工作流共享）
- [x] 权限模型 (MANUAL/AUTO/PLAN/BYPASS — 4级权限，高风险节点审批)
- [x] Hook 系统 (11 种事件类型 — 节点执行前后、工作流生命周期、权限拦截)
- [x] 会话持久化 (SQLite — 保存/恢复/分叉工作流，自动清理)
- [x] 流式事件 (EventEmitter — 发布订阅、SSE 推送、缓冲回放)
- [x] MCP stdio 传输 (stdin/stdout JSON-RPC 2.0，供 Claude Code 集成)
- [x] Desk RPC IPC (Unix Domain Socket JSON-RPC 2.0，供 Fusion-Studio 集成)
- [x] Agent 真实执行器 (Node/Workflow/MLX/Shell — 替换模拟执行)

### V0.4 ✅
- [x] 对比基准模块 (CapabilityMatrix — 32项能力、对等率、分类对比)
- [x] BenchmarkRunner (节点/工作流计时、warmup+repeats、汇总统计)
- [x] ReportRenderer (Markdown/HTML/JSON 对比报告 — Cowork vs Desk)
- [x] CLI benchmark 命令 (`fusion-cowork benchmark report/run`)
- [x] 端到端集成测试 (MCP全链路、DeskRPC全链路、Workflow+Permission+Hook+Session+Event)
- [x] DeskRPC 事件/会话/权限处理器 (9个新方法)
- [x] CLI 权限命令 (`fusion-cowork permission level/approve/deny/list`)

### V0.5 ✅
- [x] Computer Use 节点 (鼠标移动/点击、键盘输入/快捷键、Computer Use 循环)
- [x] pyobjc + AppleScript 双后端鼠标/键盘控制
- [x] AI 驱动 Computer Use 循环 (截图 → 分析 → 操作 → 重复)
- [x] 远程控制服务器/客户端 (WebSocket、认证令牌、工作流提交)
- [x] 结构化输出 (OutputSchema + NodeResult.validate)
- [x] CLI 命令: computer-use move/click/type/shortcut/run, remote serve/connect/submit, schema validate/check

### V0.6 ✅ (M5 — 执行强骨)
- [x] AgentRuntime — 每个智能体独立执行环境，消息循环 + 收件箱队列
- [x] CoordinatorExecutor — 通过编排器分派子任务到执行智能体
- [x] AgentMessageBus 集成 — 发布/订阅智能体间通信
- [x] Hook 生命周期集成 — HookManager 优先级系统，新事件 (SESSION_START, SESSION_END, PRE_COMPACT)
- [x] Hook 驱动权限 — 通过 PERMISSION_REQUEST hook 上下文自动批准/拒绝
- [x] PermissionManager 异步检查 — 支持规则匹配前的 hook 拦截
- [x] FusionCoworkSDK — 异步 HTTP 客户端 + 本地回退，编程式访问
- [x] HeadlessRunner — 无 CLI/GUI 工作流执行引擎
- [x] SDK 懒加载导入 — FusionCoworkSDK、HeadlessRunner 通过 fusion_cowork 包访问

### V0.7 ✅ (M6 — 协作空间)
- [x] Space 模型与枚举 — Space, SpaceMember, SpaceMessage, SpaceSnapshot, SpaceConfig, SpaceRole/SpaceStatus
- [x] SpaceStore — aiosqlite + SQLite WAL，9 张表 (spaces, members, messages, comments, agents, snapshots, invites, workflows, artifacts, sync_events)
- [x] SpaceService — CRUD + 归档/解档 + get_or_create + 权限门控操作
- [x] SpacePermission — 4 级角色矩阵 (Owner/Admin/Member/Viewer) + 权限校验 + 角色查询
- [x] SpaceMemberService — invite/join/leave/update_role/remove，邀请码支持过期与次数限制
- [x] CLI space 命令 — `fusion-cowork space create/list/get/archive`，`fusion-cowork space member list/invite/join/remove`
- [x] DeskRPC space 处理器 — 11 个 JSON-RPC 方法 (desk.space.create/list/get/update/archive/delete, desk.space.member.invite/join/list/remove/update_role)
- [x] 58 项测试覆盖 models/store/service/permission/member_service

### V0.7.1 ✅ (M7 — 共享对话 + 知识库绑定)
- [x] SpaceChatService — 共享上下文 + Agent 回复 + 流式推理 (SSE)
- [x] SpaceKBService — fusion-kb 绑定 + 文档管理 + RAG 搜索/查询
- [x] SpaceAPI — 18 个 REST 端点 + SSE 事件流 (`/spaces/{id}/stream`)
- [x] FusionMLXClient 增强 — 端口修正 (11434) + 瞬态错误重试 + 流式健壮性
- [x] KBClient 完善 — create_kb/delete_kb/upload_file/list_documents
- [x] SharedContext — 工作流节点访问空间消息 + KB 搜索/查询
- [x] CLI 扩展 — `space chat` 交互式对话 + `space knowledge bind/status/upload/search/unbind`
- [x] DeskRPC 处理器 — 9 个新方法 (desk.space.chat.send/list/context, desk.space.knowledge.bind/status/upload/search/query/unbind)
- [x] 52 项 M7 测试 (总计 461)

### V0.8.0 ✅ (M8 — Agent 编排 + 多 Agent 接力)
- [x] SpaceAgentRuntime — 空间级 Agent 运行时，支持隔离 (add/remove/list/get/call/chain/register)
- [x] AgentStudioClient — Agent Studio API HTTP 客户端，支持重试 + 导入到空间
- [x] Chat 接力 — SpaceChatService.relay_agents() 顺序多 Agent 链式执行
- [x] SpacePermission — `call_agent` 权限 (owner/admin/member: ✅, viewer: ❌)
- [x] SpaceAPI — 6 个新 Agent 端点 (GET/POST /agents, GET/DELETE /agents/{id}, POST /agents/call, POST /agents/relay)
- [x] Orchestrator 桥接 — register_to_orchestrator() 将空间 Agent 映射为 Agent dataclass
- [x] CLI — `space agent list/add/remove/call/relay` 命令
- [x] DeskRPC — 5 个新 JSON-RPC 方法 (desk.space.agent.list/add/remove/call/relay)
- [x] 35 项 M8 测试 (总计 490)
- [x] KBClient 端口修正 (11432→11436, issue #6)
- [x] desk.project.syncKnowledge — 接收外部知识库文件同步 (issue #7)
- [x] desk.project.importSnapshot — 接收会话快照导入 (issue #8)
- [x] desk.project.exportToProject — 导出空间内容到 fusion-projects (issue #9)

### V0.8.1 ✅ (Issue #3/#4 — Artifact 权限 + FSB 集成)
- [x] SpaceArtifactService — Artifact CRUD + 所有权追踪 + 权限校验 (issue #3)
- [x] 权限矩阵 — 15 个动作 (新增: view/edit/share/transfer_artifact)
- [x] DeskRPC — 7 个 desk.space.artifact.* 处理器
- [x] ModuleRegistry — 侧边栏模块注册 (register/list/enable/disable) (issue #4)
- [x] NotificationService — 审批任务通知推送 (SSE + desk.notification.push) (issue #4)
- [x] DeskRPC — 4 个 desk.module.* + 3 个 desk.notification.* 处理器
- [x] Store 迁移 — space_artifacts 扩展列 + 2 张新表 (sidebar_modules, space_notifications)
- [x] 29 项新测试 (18 artifact + 11 FSB, 总计 519)

### 补丁版本

#### V0.1.9 (补丁)
- [x] 从 git 追踪中移除本地文档（6 个文件 → .gitignore）
- [x] feat: ast_diff 模块从 fusion-multi-node 迁移 (#26, #27)

#### V0.1.8 (补丁)
- [x] 端口规范化: 9760→11437, 9761→11438, 9762→11439 (#24, #25)
- [x] Ruff lint: 修复 desk_rpc 处理器中 4 个未使用变量

#### V0.1.7 (补丁)
- [x] 19 个缺失 RPC handler 补全，兼容 Studio GUI (#19, #23)
- [x] P0: desk.space.chat.stream — 流式对话
- [x] P1: agent.update, 快照 CRUD (5), 评论 CRUD (2)
- [x] P2: 工作流 (3), 发现扫描, 桌面共享 (2), 会话快照 (5)
- [x] Store 层: update_agent, remove_agent, 快照 get/delete, 评论 CRUD
- [x] RPC 方法总数: 79 → 99

#### V0.1.6 (补丁)
- [x] RPC 别名: `desk.space.chat.history` → chat.list (Studio 兼容, #20)
- [x] RPC 别名: `desk.space.notification.*` → notification.* (Studio 兼容, #21)
- [x] GUI 差距审计: 19 个缺失 RPC handler 已归档 (#19)

#### V0.1.5 (补丁)
- [x] start.sh 生命周期管理 (start/stop/restart/status/log/doctor/clean)
- [x] `python -m fusion_cowork` 现在可用 (__main__.py)
- [x] 移除 desk.mlx.start/stop — 产品层不应管理基础设施 (#16)
- [x] 修正 pyproject.toml 描述 (#16)

#### V0.1.4 (补丁)
- [x] CI: GitHub Actions 工作流 (ruff lint + pytest + coverage)
- [x] Lint: ruff 配置完成，F 系列缺陷已修复，0 问题
- [x] 测试: 519 项测试全通过
- [x] 文档: 环境变量说明、CI 徽章

### V0.8 (规划中)
- [ ] 可视化工作流编辑器 (Fusion-Studio GUI)
- [ ] 插件系统 (第三方节点包)
- [ ] 云备份与恢复 (可选，加密)
- [ ] 移动端伴侣 App (远程触发，通过 WebSocket)

---

## 🔒 安全与隐私

- **100% 本地离线** — 所有操作在本地执行，零文件上传
- **无遥测** — 无网络请求、无数据上报、无分析
- **预览模式** — 所有操作支持 `--dry-run` 预览
- **可撤销** — 删除操作默认移到废纸篓
- **完整审计** — 执行日志和复盘报告

---

## 🤝 参与贡献

欢迎贡献！请先阅读[贡献指南](CONTRIBUTING.md)。

---

## 📄 开源协议

本项目基于 Apache License 2.0 开源。详见 [LICENSE](LICENSE)。

---

## 架构影响来源

Fusion-Cowork 基于以下开源项目的模式构建：

| 模式 | 来源 | 整合位置 |
|------|------|----------|
| 工具注册表 + 类型强制转换 | [Squish](https://github.com/nicepkg/squish) `tool_registry.py` | `engine/node.py` — `_coerce_int/bool/number/array` |
| Lazy Import `__getattr__` | [Squish](https://github.com/nicepkg/squish) `__init__.py` | `fusion_cowork/__init__.py` — `_LAZY_IMPORTS` |
| 工具名称映射 | [Squish](https://github.com/nicepkg/squish) `tool_name_map.py` | `NODE_NAME_ALIASES` + `register_alias` |
| 内置工具集 | [Squish](https://github.com/nicepkg/squish) `builtin_tools.py` | 5 个工具节点：Shell、Python、Web 搜索、Fetch、Edit |
| 工作流引擎 (DAG) | [n8n](https://github.com/n8n-io/n8n) | `engine/workflow.py` — 拓扑排序、数据传递 |
| MCP 协议 | [LibreChat](https://github.com/danny-avila/LibreChat) | 计划 V0.2 实现 |

---

<p align="center">
  <strong>Fusion-Cowork — 让 Mac 自己干活，本地 AI 全自动桌面办公</strong>
</p>
<p align="center">
  <sub>Built with ❤️ by Fusion-MLX Team</sub>
</p>
