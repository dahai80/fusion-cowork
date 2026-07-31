<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>Local-first, zero-code desktop automation platform for macOS Apple Silicon</strong></p>
  <p><em>Let your Mac do the work — 100% offline, AI-powered, privacy-first.</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/Offline-First-important" alt="Offline">
  <img src="https://img.shields.io/badge/status-beta-yellow" alt="Beta">
</p>

---

## 📋 Overview

**Fusion-Cowork** is one of the three flagship products in the [Fusion-MLX](https://github.com/fusion-mlx) Apple Silicon local AI ecosystem. It provides a zero-code desktop intelligent automation platform for all office users.

### Ecosystem Positioning

| Product | Role | Audience |
|---------|------|----------|
| **Fusion-Code** | Developer coding agent | Programmers |
| **Fusion-Agent-Studio** | Advanced agent workflow orchestration | Developers / Architects |
| **Fusion-Cowork** 🎯 | Desktop automation for everyone | **All office users** |

> **Studio builds workflows, Desk runs them, Code writes capabilities, KB stores knowledge, Hub manages models.**

### Key Differentiators

- ✅ **100% Local & Offline** — Zero file uploads, zero privacy leaks, zero telemetry
- ✅ **macOS Native** — Deep integration with Desktop, Downloads, Documents directories
- ✅ **MLX Hardware Acceleration** — Apple Silicon native inference via fusion-mlx
- ✅ **AI Semantic Understanding** — Understands content, not just filenames
- ✅ **Zero Code** — Describe your need in plain language, get an automation workflow
- ✅ **Full Ecosystem Integration** — Seamless with Fusion-Code, Agent-Studio, Fusion-KB, Model-Hub

---

## 🚀 Quick Start

### Installation

```bash
# Clone
git clone https://github.com/dahai80/fusion-cowork.git
cd fusion-cowork

# Install with pip
pip install -e .

# Install with AI support (optional, recommended)
pip install -e ".[web]"

# Or use uv (faster)
uv venv --python 3.11
uv pip install -e ".[test]"
```

### Basic Usage

```bash
# List available templates
fusion-cowork template list

# Show template details
fusion-cowork template show desktop_daily_cleanup

# Run a template (preview mode)
fusion-cowork template run desktop_daily_cleanup --dry-run

# Run a template (execute)
fusion-cowork template run desktop_daily_cleanup

# Generate a workflow with AI
fusion-cowork ai generate "Organize all PDFs on my desktop by topic"

# Check AI service status
fusion-cowork ai status

# System information
fusion-cowork system info

# Start MCP server (stdio mode for Claude Code)
fusion-cowork mcp serve

# Start MCP server (HTTP mode)
fusion-cowork mcp serve --transport http --port 9761

# Start Desk RPC server (for Fusion-Studio GUI)
fusion-cowork desk rpc

# Session management
fusion-cowork session list
fusion-cowork session show <session_id>
fusion-cowork session fork <session_id> --from-step 2
fusion-cowork session cleanup --days 30

# Permission management
fusion-cowork permission level <manual|auto|plan|bypass>
fusion-cowork permission approve <tool_name> --scope <scope>
fusion-cowork permission deny <tool_name> --scope <scope>
fusion-cowork permission list

# Benchmark — 功能对比报告
fusion-cowork benchmark report --format markdown|html|json [-o report.md]
fusion-cowork benchmark run --node file_input --node shell_exec --repeats 3

# Plugin management (M3)
fusion-cowork plugin list
fusion-cowork plugin install /path/to/plugin
fusion-cowork plugin load <name>
fusion-cowork plugin uninstall <name>

# Skill management (M3)
fusion-cowork skill list
fusion-cowork skill run /cleanup
fusion-cowork skill search "clean"

# Chrome CDP — remote browser control (M3)
fusion-cowork cdp navigate https://example.com
fusion-cowork cdp snapshot
fusion-cowork cdp click 42
fusion-cowork cdp fill --selector "#search" --value "hello"
fusion-cowork cdp screenshot --save ~/Desktop/shot.png
fusion-cowork cdp evaluate "document.title"
```

---

## 🎯 Built-in Templates (10 ready-to-use)

| Template | Category | Description | Needs AI |
|----------|----------|-------------|----------|
| 🧹 **Desktop Daily Cleanup** | Desktop | Organize desktop files by type | ❌ |
| 📥 **Download Organizer** | File | Archive, deduplicate, and clean Downloads | ❌ |
| 📝 **Document Batch Summarizer** | AI | Batch summarize PDFs/Word/Markdown | ✅ |
| 🏷️ **AI Smart File Classification** | AI | Classify files by semantic content | ✅ |
| ✏️ **AI Batch Rename** | AI | Generate meaningful filenames from content | ✅ |
| 💾 **Disk Space Cleaner** | System | Scan and clean caches, temp files | ❌ |
| 📂 **Project File Collector** | File | Gather project files by type | ❌ |
| 🔍 **Duplicate File Scanner** | System | Find and clean duplicate files | ❌ |
| 📋 **Log Cleaner** | System | Scan and clean log files | ❌ |
| 🖼️ **Image Batch Rename** | File | Batch rename and classify images | ❌ |

### Industry Templates (V0.3)

| Template | Industry | Description |
|----------|----------|-------------|
| 🎨 **Design Asset Organizer** | Design | Auto-classify PSD/AI/Sketch files |
| 🖼️ **Design Batch Export** | Design | Batch export design assets |
| 💻 **Project Code Cleanup** | Dev | Clean caches, pycache, node_modules |
| 🔍 **Log Analyzer** | Dev | AI-powered log analysis |
| 🚀 **Project Scaffold** | Dev | Auto-create project structure |
| 📊 **CSV Data Cleaner** | Data | Deduplicate, normalize CSV |
| 🔄 **Format Converter** | Data | Batch CSV↔JSON↔Excel |
| 🖥️ **System Inspector** | Ops | Collect system health report |
| 💾 **Backup Manager** | Ops | Scheduled file backup |
| ⚠️ **Disk Alert** | Ops | Monitor disk usage, auto-clean |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                     UI Layer                          │
│   CLI (click)  │  Web UI (FastAPI)  │  macOS (.app)  │
├─────────────────────────────────────────────────────┤
│                 Workflow Engine Layer                  │
│   WorkflowEngine  │  TaskScheduler  │  NodeRegistry   │
│   (DAG, n8n-inspired) │  (APScheduler) │  (28 nodes)   │
├─────────────────────────────────────────────────────┤
│                   AI Capability Layer                  │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (Natural language → Wf)     │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 System Capability Layer                │
│   macOS nodes (AppleScript / osascript)               │
│   File ops  │  Desktop mgmt  │  Disk cleanup  │ Tools  │
└─────────────────────────────────────────────────────┘
```

### Node Types (33 nodes built-in)

| Category | Count | Nodes |
|----------|-------|-------|
| `macos_system` | 15 | Desktop Clean, Download Organizer, Disk Cleaner, File Watcher, File Classifier, Batch Rename, Copy, Move, Delete, Find, **Mouse Move, Mouse Click, Keyboard Type, Keyboard Shortcut, Computer Use Loop** 🆕 |
| `ai_processing` | 4 | AI Classify, AI Summarize, AI Generate Name, **OCR** 🆕 |
| `tool` | 5 | Shell Exec, Python REPL, Web Search, Fetch URL, Apply Edit |
| `browser` | 3 | Browser Open, Browser Extract, Browser Automate |
| `cdp` | 10 | CDP Navigate, Snapshot, Click, Fill, Fill Form, Screenshot, Evaluate, Emulate, Network, Console |
| `io` | 2 | File Input, File Output |
| `logic` | 3 | Filter, Loop, Merge |
| **Claude Cowork parity** | **6** | **Screen Capture, Clipboard, Notification, App Lifecycle, OCR, MCP Server** |

### Claude Cowork Parity (V0.2) 🆕

| Capability | Status | Implementation |
|------------|--------|---------------|
| Screen capture & desktop view | ✅ | `ScreenCaptureNode` — full/selection/window screenshot |
| Clipboard read/write | ✅ | `ClipboardNode` — pbpaste/pbcopy |
| System notifications | ✅ | `NotificationNode` — macOS Notification Center |
| macOS app lifecycle | ✅ | `AppLifecycleNode` — launch/quit/activate/list |
| OCR / screen text recognition | ✅ | `OCRNode` — Vision + fusion-mlx |
| MCP protocol server | ✅ | `MCPServer` — 15 tools for Claude Desktop/Code |

### Ecosystem Integration

| Component | Protocol | Purpose |
|-----------|----------|---------|
| **fusion-mlx** | HTTP API (port 8000) | LLM inference, text generation, embeddings |
| **Fusion-KB** | HTTP API (port 11434) | Knowledge base semantic search, RAG |
| **Fusion-Code** | Auto-generated scripts | Complex logic execution |
| **Agent-Studio** | Workflow import | Advanced pre-built workflows |
| **Model-Hub** | Model dispatch | Auto-select optimal local model |

---

## 🔧 Node Reference

### macOS System Nodes

| Node | Description |
|------|-------------|
| `desktop_clean` | Organize desktop files by type/date |
| `download_organizer` | Archive, deduplicate, and clean Downloads |
| `disk_cleaner` | Scan and clean cache, temp files, `.DS_Store` |
| `file_watcher` | Watch directory for changes, trigger actions |
| `screen_capture` 🆕 | Take desktop screenshot (full/selection/window) |
| `clipboard` 🆕 | Read/write/clear system clipboard |
| `notification` 🆕 | Send macOS Notification Center alerts |
| `app_lifecycle` 🆕 | Launch/quit/activate/list macOS applications |
| `ocr` 🆕 | Recognize text from images (Vision + MLX) |

### AI Processing Nodes (`→ fusion-mlx`)

| Node | Function | Backend |
|------|----------|---------|
| `ai_classify` | Semantic file classification | `fusion-mlx /v1/chat/completions` |
| `ai_summarize` | Document summarization & report | `fusion-mlx /v1/chat/completions` |
| `ai_generate_name` | Intelligent file naming | `fusion-mlx /v1/chat/completions` |

### Tool Nodes (from Squish built-in tools)

| Node | Description |
|------|-------------|
| `shell_exec` | Execute shell commands, capture output |
| `python_repl` | Execute Python code in isolated subprocess |
| `web_search` | Search web via DuckDuckGo Lite |
| `fetch_url` | Fetch URL content, auto-extract text |
| `apply_edit` | Find-and-replace edits on files |

### IO / Logic Nodes

| Node | Description |
|------|-------------|
| `file_input` | Read file list or directory contents |
| `file_output` | Write workflow results to files |
| `filter` | Filter data by conditions (extension, size, date) |
| `loop` | Batch process each item in a list |
| `merge` | Merge data from multiple upstream nodes |

### Chrome CDP Nodes 🆕 (M3)

| Node | Description |
|------|-------------|
| `cdp_navigate` | Navigate Chrome to URL via CDP |
| `cdp_snapshot` | Get page accessibility tree |
| `cdp_click` | Click element by backendNodeId |
| `cdp_fill` | Fill form field by CSS selector |
| `cdp_fill_form` | Batch fill multiple form fields |
| `cdp_screenshot` | Capture page screenshot (PNG) |
| `cdp_evaluate` | Execute JavaScript in page |
| `cdp_emulate` | Emulate device viewport |
| `cdp_network` | Query network requests |
| `cdp_console` | Query console messages |

### Plugin System 🆕 (M3)

Plugin system allows extending Fusion-Cowork with custom nodes:

```python
# Plugin manifest (manifest.json)
{
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "Custom nodes",
    "nodes": ["my_custom_node"],
    "entry_point": "plugin"
}
```

```bash
# Install from directory or zip
fusion-cowork plugin install /path/to/plugin_dir
fusion-cowork plugin install /path/to/plugin.zip
```

### Skill Mechanism 🆕 (M3)

Skills are high-level shortcuts that map to existing nodes:

| Skill | Alias | Node |
|-------|-------|------|
| `/cleanup` | 清理桌面 | `desktop_clean` |
| `/classify` | AI分类 | `ai_classify` |
| `/screenshot` | 截图 | `screen_capture` |
| `/search` | 搜索 | `web_search` |
| `/organize` | 下载整理 | `download_organizer` |
| `/diskclean` | 磁盘清理 | `disk_cleaner` |

MCP tools `skill_list` and `skill_run` are available for Claude Desktop/Code integration.

### Computer Use Nodes 🆕 (M4)

Mouse/keyboard control + AI loop for autonomous desktop operation:

| Node | Description |
|------|-------------|
| `mouse_move` | Move mouse to (x, y) coordinates |
| `mouse_click` | Click (left/right/double) at position |
| `keyboard_type` | Type text via keyboard |
| `keyboard_shortcut` | Press key combo (Cmd+C, etc.) |
| `computer_use_loop` | Screenshot → AI analyze → act → repeat loop |

```bash
# Move mouse
fusion-cowork computer-use move 500 300

# Click
fusion-cowork computer-use click --x 500 --y 300 --button left

# Type text
fusion-cowork computer-use type "Hello World"

# Keyboard shortcut
fusion-cowork computer-use shortcut c --modifiers cmd

# AI-powered Computer Use loop
fusion-cowork computer-use run "打开 Safari 并搜索天气" --max-steps 10
```

### Remote Control 🆕 (M4)

WebSocket-based remote control for external clients:

```bash
# Start remote server
fusion-cowork remote serve --port 9762 --token mytoken

# Connect from another machine
fusion-cowork remote connect ws://host:9762/control --token mytoken

# Submit workflow remotely
fusion-cowork remote submit workflow.json --url ws://host:9762/control
```

### Structured Output 🆕 (M4)

JSON Schema validation for node outputs:

```bash
# Validate data against schema
fusion-cowork schema validate data.json schema.json

# Check node output schema
fusion-cowork schema check mouse_move
```

NodeResult now supports `schema` field and `validate()` method for automatic output validation.

---

## 📝 Workflow Examples

### Desktop Daily Cleanup (JSON)

```json
{
  "name": "Desktop Daily Cleanup",
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

### AI-Generated Workflow

```bash
fusion-cowork ai generate "Every night at 9pm, clean my Downloads folder and back it up to Documents"
```

---

## 🧪 Running Tests

```bash
# Install test dependencies
pip install -e ".[test]"

# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=fusion_cowork --cov-report=html
```

---

## 🛣️ Roadmap

### V0.1 (MVP) ✅
- [x] Core file automation (Desktop, Downloads, Documents)
- [x] AI batch processing (classify, summarize, rename)
- [x] Template center (10 built-in templates)
- [x] Natural language → workflow generation
- [x] Ecosystem integration (fusion-mlx, Fusion-KB)
- [x] Type coercion for LLM-generated params (from Squish)
- [x] Lazy import architecture (from Squish)
- [x] Tool nodes: Shell, Python REPL, Web Search, Fetch URL, Apply Edit

### V0.2 ✅
- [x] Enhanced scheduler (Cron UI, calendar view, task dependency, stats)
- [x] AI workflow optimizer (bottleneck detection, auto-fix, fusion-mlx suggestions)
- [x] Batch report generator (Markdown/HTML, multi-workflow summary)
- [x] macOS native .app packaging script
- [x] Screen capture (desktop screenshot with AI analysis)
- [x] Clipboard integration (read/write/clear)
- [x] System notifications (macOS Notification Center)
- [x] macOS app lifecycle (launch/quit/activate/list)
- [x] OCR text recognition (Vision + fusion-mlx)
- [x] MCP server mode (15 tools for Claude Desktop/Code)

### V0.3 ✅
- [x] Industry-specific templates (design/dev/data/ops — 10 templates)
- [x] Multi-agent orchestration (agent registration, task decomposition, parallel execution)
- [x] Cross-device collaboration (WebSocket sync, device discovery, workflow sharing)
- [x] Permission model (MANUAL/AUTO/PLAN/BYPASS — 4-tier, high-risk node approval)
- [x] Hook system (11 event types — pre/post node, workflow lifecycle, permission intercept)
- [x] Session persistence (SQLite — save/resume/fork workflows, auto-cleanup)
- [x] Streaming events (EventEmitter — pub/sub, SSE push, buffer replay)
- [x] MCP stdio transport (JSON-RPC 2.0 over stdin/stdout for Claude Code)
- [x] Desk RPC IPC (JSON-RPC 2.0 over Unix Domain Socket for Fusion-Studio)
- [x] Agent real executors (Node/Workflow/MLX/Shell — replace simulated execution)

### V0.4 ✅
- [x] Benchmark module (CapabilityMatrix — 32 capabilities, parity scoring, category breakdown)
- [x] BenchmarkRunner (node/workflow timing, warmup+repeats, summary stats)
- [x] ReportRenderer (Markdown/HTML/JSON comparison reports — Cowork vs Desk)
- [x] CLI benchmark commands (`fusion-cowork benchmark report/run`)
- [x] E2E integration tests (MCP full chain, DeskRPC full chain, Workflow+Permission+Hook+Session+Event)
- [x] DeskRPC event/session/permission handlers (9 new methods)
- [x] CLI permission commands (`fusion-cowork permission level/approve/deny/list`)

### V0.5 ✅
- [x] Computer Use nodes (MouseMove, MouseClick, KeyboardType, KeyboardShortcut, ComputerUseLoop)
- [x] pyobjc + AppleScript dual-backend for mouse/keyboard control
- [x] AI-powered Computer Use loop (screenshot → analyze → act → repeat)
- [x] Remote control server/client (WebSocket, auth token, workflow submission)
- [x] Structured output schema (OutputSchema + NodeResult.validate)
- [x] CLI commands: computer-use move/click/type/shortcut/run, remote serve/connect/submit, schema validate/check

### V0.6 ✅ (M5 — Execution Strengthening)
- [x] AgentRuntime — independent execution environment per agent with message loop & inbox queue
- [x] CoordinatorExecutor — dispatches subtasks to executor agents via orchestrator
- [x] AgentMessageBus integration — pub/sub inter-agent communication
- [x] Hook lifecycle integration — HookManager priority system, new events (SESSION_START, SESSION_END, PRE_COMPACT)
- [x] Hook-driven permission — auto-approve/deny via PERMISSION_REQUEST hook context
- [x] PermissionManager async check — supports hook interception before rule matching
- [x] FusionCoworkSDK — async HTTP client with local fallback for programmatic access
- [x] HeadlessRunner — no-CLI/no-GUI workflow execution engine
- [x] SDK lazy imports — FusionCoworkSDK, HeadlessRunner accessible via fusion_cowork package

### V0.7 (Planned)
- [ ] Visual workflow editor (Fusion-Studio GUI)
- [ ] Plugin system (3rd-party node packages)
- [ ] Cloud backup & restore (optional, encrypted)
- [ ] Mobile companion app (remote trigger via WebSocket)

---

## 🔒 Security & Privacy

- **100% Local & Offline** — All operations run locally, zero file uploads
- **No Telemetry** — No network requests, no data reporting, no analytics
- **Preview Mode** — All operations support `--dry-run` preview
- **Undo Support** — Deletions default to Trash (not permanent)
- **Full Audit Trail** — Complete execution logs and review reports

---

## 🤝 Contributing

Contributions are welcome! Please read our [contributing guidelines](CONTRIBUTING.md) first.

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Architecture Influences

Fusion-Cowork builds upon patterns from the open-source ecosystem:

| Pattern | Source | Integration |
|---------|--------|-------------|
| Tool Registry + type coercion | [Squish](https://github.com/nicepkg/squish) `tool_registry.py` | `engine/node.py` — `_coerce_int/bool/number/array` |
| Lazy import via `__getattr__` | [Squish](https://github.com/nicepkg/squish) `__init__.py` | `fusion_cowork/__init__.py` — `_LAZY_IMPORTS` |
| Tool name alias mapping | [Squish](https://github.com/nicepkg/squish) `tool_name_map.py` | `NODE_NAME_ALIASES` + `register_alias` |
| Built-in tool set | [Squish](https://github.com/nicepkg/squish) `builtin_tools.py` | 5 tool nodes: Shell, Python, Web Search, Fetch, Edit |
| Workflow engine (DAG) | [n8n](https://github.com/n8n-io/n8n) | `engine/workflow.py` — topological sort, data passing |
| MCP protocol | [LibreChat](https://github.com/danny-avila/LibreChat) | Planned for V0.2 |

---

<p align="center">
  <strong>Fusion-Cowork — Let your Mac do the work, locally and privately.</strong>
</p>
<p align="center">
  <sub>Built with ❤️ by the Fusion-MLX Team</sub>
</p>

---

<br>

<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>macOS 原生、纯本地离线、零代码桌面智能自动化平台</strong></p>
  <p><em>让 Mac 自己干活，本地 AI 全自动桌面办公</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="许可证">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/离线优先-核心特性-important" alt="离线优先">
  <img src="https://img.shields.io/badge/状态-beta-yellow" alt="Beta">
</p>

---

## 📋 产品简介

**Fusion-Cowork** 是 Fusion-MLX 全栈 Apple Silicon 本地 AI 生态的三大旗舰核心产品之一，面向所有办公用户提供零代码桌面智能自动化能力。

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
fusion-cowork mcp serve --transport http --port 9761

# 启动 Desk RPC 服务 (供 Fusion-Studio GUI 调用)
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

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────┐
│                     UI 层                              │
│   CLI (click)  │  Web UI (FastAPI)  │  macOS (.app)   │
├─────────────────────────────────────────────────────┤
│                  流程引擎层                             │
│   WorkflowEngine  │  TaskScheduler  │  NodeRegistry   │
│   (DAG, n8n 启发)  │  (APScheduler)  │  (23 个节点)    │
├─────────────────────────────────────────────────────┤
│                   AI 能力层                             │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (自然语言 → 工作流)          │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 系统能力层                              │
│   macOS 节点 (AppleScript / osascript)                │
│   文件操作  │  桌面整理  │  磁盘清理  │  通用工具       │
└─────────────────────────────────────────────────────┘
```

### 节点类型（23 个内置节点）

| 分类 | 数量 | 节点 |
|------|------|------|
| `macos_system` | 4 | 桌面清理、下载整理、磁盘清理、文件监听 |
| `file_operation` | 6 | 文件分类、批量重命名、复制、移动、删除、查找 |
| `ai_processing` | 3 | AI 分类、AI 摘要、AI 重命名 |
| `tool` 🆕 | 5 | Shell 命令、Python REPL、Web 搜索、获取网页、文件编辑 |
| `io` | 2 | 文件输入、文件输出 |
| `logic` | 3 | 条件过滤、循环处理、数据合并 |

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

### V0.2 (规划中)
- [ ] 自定义可视化工作流编辑器
- [ ] 高级定时任务管理（Cron UI）
- [ ] 批量报表生成
- [ ] AI 流程自动优化
- [ ] macOS 原生 .app 打包

### V0.3 ✅
- [x] 行业自动化模板
- [x] 多智能体联动
- [x] 跨设备协同
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

本项目基于 MIT 许可证开源。详见 [LICENSE](LICENSE)。

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