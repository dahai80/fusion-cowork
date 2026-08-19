[English](README.md) | [中文](README_CN.md)

---

<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>Local-first, zero-code desktop automation platform for macOS Apple Silicon</strong></p>
  <p><em>Let your Mac do the work — 100% offline, AI-powered, privacy-first.</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/Offline-First-important" alt="Offline">
  <img src="https://img.shields.io/badge/status-beta-yellow" alt="Beta">
  <img src="https://github.com/dahai80/fusion-cowork/actions/workflows/ci.yml/badge.svg" alt="CI">
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
fusion-cowork mcp serve --transport http --port 11438

# Start Desk RPC server (for Fusion-Studio GUI)
fusion-cowork desk rpc

# Lifecycle management via start.sh
./start.sh start    # Start desk RPC daemon
./start.sh stop     # Graceful stop
./start.sh restart  # Stop + start
./start.sh status   # PID, socket, memory, uptime
./start.sh log [-f] # Tail logs (-f to follow)
./start.sh doctor   # Health check (venv, CLI, socket, upstream services)
./start.sh clean    # Rotate logs, clear __pycache__
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FUSION_MLX_API_KEY` | `local` | **fusion-gateway client api key** (a key from `auth.api_keys[].key` in fusion-gateway `config.yaml`). NOT the fusion-mlx `settings.json` `auth.api_key` — gateway and mlx use separate auth. |
| `FUSION_MLX_URL` | `http://localhost:11432/v1` | fusion-mlx base URL (via fusion-gateway netlayer) |
| `FUSION_RAG_URL` | `http://localhost:11436` | fusion-rag (fusion-kb) base URL |

> **鉴权说明**: fusion-cowork 通过 fusion-gateway(:11432) 调 fusion-mlx,存在**两套独立鉴权**:
> 1. **gateway client key** — 客户端请求 gateway 时用,即本变量 `FUSION_MLX_API_KEY`(取自 gateway `config.yaml` 的 `auth.api_keys[].key`)
> 2. **mlx backend key** — gateway 转发到 mlx 时用,配置在 gateway `config.yaml` 的 `backends.fusion-mlx.api_key`,客户端无需关心
>
> 常见 401 错误:把 mlx 的 key 填进了 `FUSION_MLX_API_KEY`。应填 gateway 的 client key。

```bash
# Example: set API key to a fusion-gateway client key (from config.yaml auth.api_keys)
export FUSION_MLX_API_KEY="fg-admin-key"
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

# Benchmark
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

# Collaboration Space (M6+M7)
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
│   (DAG, n8n-inspired) │  (APScheduler) │  (47 nodes)   │
├─────────────────────────────────────────────────────┤
│                   AI Capability Layer                  │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (Natural language → Wf)     │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 System Capability Layer                │
│   macOS nodes (AppleScript / osascript)               │
│   File ops  │  Desktop mgmt  │  Disk cleanup  │ Tools  │
├─────────────────────────────────────────────────────┤
│              Collaboration Space Layer 🆕              │
│   SpaceStore (SQLite WAL)  │  SpaceService (CRUD)     │
│   SpacePermission (4-tier)  │  SpaceMemberService     │
│   SpaceChatService (SSE)   │  SpaceKBService (RAG)    │
│   SharedContext (nodes)     │  SpaceAPI (REST+SSE)     │
└─────────────────────────────────────────────────────┘
```

### Node Types (47 nodes built-in, 7 categories)

| Category | Count | Nodes |
|----------|-------|-------|
| `macos_system` | 13 | Desktop Clean, Download Organizer, Disk Cleaner, File Watcher, Screen Capture, Clipboard, Notification, App Lifecycle, OCR, **Mouse Move, Mouse Click, Keyboard Type, Keyboard Shortcut, Computer Use Loop** 🆕 |
| `ai_processing` | 4 | AI Classify, AI Summarize, AI Generate Name, AI Vision Analyze |
| `tool` | 9 | Shell Exec, Python REPL, Web Search, Fetch URL, Apply Edit, Browser Open, Browser Extract, Browser Automate, Trainer Node |
| `file_operation` | 6 | File Classifier, Batch Rename, Copy, Move, Delete, Find |
| `io` | 2 | File Input, File Output |
| `logic` | 3 | Filter, Loop, Merge |
| `fusion_ecosystem` | 1 | Trainer (Fusion-Trainer 互通) |
| **CDP browser nodes** | **10** | CDP Navigate, Snapshot, Click, Fill, Fill Form, Screenshot, Evaluate, Emulate, Network, Console (`tool` 分类下注册) |

### Claude Cowork Parity (V0.2) 🆕

| Capability | Status | Implementation |
|------------|--------|---------------|
| Screen capture & desktop view | ✅ | `ScreenCaptureNode` — full/selection/window screenshot |
| Clipboard read/write | ✅ | `ClipboardNode` — pbpaste/pbcopy |
| System notifications | ✅ | `NotificationNode` — macOS Notification Center |
| macOS app lifecycle | ✅ | `AppLifecycleNode` — launch/quit/activate/list |
| OCR / screen text recognition | ✅ | `OCRNode` — Vision + fusion-mlx |
| MCP protocol server | ✅ | `MCPServer` — 16 tools for Claude Desktop/Code |

### Ecosystem Integration

| Component | Protocol | Purpose |
|-----------|----------|---------|
| **fusion-mlx** | HTTP API (via fusion-gateway :11432) | LLM inference, text generation, embeddings |
| **Fusion-RAG** | HTTP API (port 11436) | Knowledge base semantic search, RAG |
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
    "entry_point": "plugin",
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
fusion-cowork remote serve --port 11439 --token mytoken

# Connect from another machine
fusion-cowork remote connect ws://host:11439/control --token mytoken

# Submit workflow remotely
fusion-cowork remote submit workflow.json --url ws://host:11439/control
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
- [x] MCP server mode (16 tools for Claude Desktop/Code)

### V0.3 ✅
- [x] Industry-specific templates (design/dev/data/ops — 10 templates)
- [x] Multi-agent orchestration (agent registration, task decomposition, parallel execution)
- [x] Cross-device collaboration (WebSocket sync, device discovery, workflow sharing)
- [x] Permission model (MANUAL/AUTO/PLAN/BYPASS — 4-tier, high-risk node approval)
- [x] Hook system (14 event types — pre/post node, workflow lifecycle, permission intercept, session, pre-compact)
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

### V0.7 ✅ (M6 — Collaboration Space)
- [x] Space models & enums — Space, SpaceMember, SpaceMessage, SpaceSnapshot, SpaceConfig, SpaceRole/SpaceStatus
- [x] SpaceStore — aiosqlite + SQLite WAL, 9 tables (spaces, members, messages, comments, agents, snapshots, invites, workflows, artifacts, sync_events)
- [x] SpaceService — CRUD + archive/unarchive + get_or_create + permission-gated operations
- [x] SpacePermission — 4-tier role matrix (Owner/Admin/Member/Viewer) + permission check + role query
- [x] SpaceMemberService — invite/join/leave/update_role/remove, invite code with expiry & max-uses
- [x] CLI space commands — `fusion-cowork space create/list/get/archive`, `fusion-cowork space member list/invite/join/remove`
- [x] DeskRPC space handlers — 11 JSON-RPC methods (desk.space.create/list/get/update/archive/delete, desk.space.member.invite/join/list/remove/update_role)
- [x] 58 tests covering models, store, service, permission, member service

### V0.7.1 ✅ (M7 — Shared Conversation + KB Binding)
- [x] SpaceChatService — shared context + Agent reply + streaming (SSE)
- [x] SpaceKBService — fusion-kb binding + document management + RAG search/query
- [x] SpaceAPI — 25 REST endpoints + SSE event stream (`/spaces/{id}/stream`)
- [x] FusionMLXClient enhancements — port fix (11432 via gateway) + retry on transient errors + stream robustness
- [x] KBClient completion — create_kb/delete_kb/upload_file/list_documents
- [x] SharedContext — workflow node access to space messages + KB search/query
- [x] CLI extensions — `space chat` interactive dialog + `space knowledge bind/status/upload/search/unbind`
- [x] DeskRPC handlers — 9 new methods (desk.space.chat.send/list/context, desk.space.knowledge.bind/status/upload/search/query/unbind)
- [x] 52 M7-specific tests (461 total)

### V0.8.0 ✅ (M8 — Agent Orchestration + Multi-Agent Relay)
- [x] SpaceAgentRuntime — space-level Agent runtime with isolation (add/remove/list/get/call/chain/register)
- [x] AgentStudioClient — HTTP client for Agent Studio API with retry + import-to-space
- [x] Chat relay — SpaceChatService.relay_agents() sequential multi-Agent chain
- [x] SpacePermission — `call_agent` action (owner/admin/member: ✅, viewer: ❌)
- [x] SpaceAPI — 6 new agent endpoints (GET/POST /agents, GET/DELETE /agents/{id}, POST /agents/call, POST /agents/relay)
- [x] Orchestrator bridge — register_to_orchestrator() maps space agents to Agent dataclasses
- [x] CLI — `space agent list/add/remove/call/relay` commands
- [x] DeskRPC — 5 new JSON-RPC methods (desk.space.agent.list/add/remove/call/relay)
- [x] 35 M8-specific tests (490 total)
- [x] KBClient port fix (11432→11436, issue #6)
- [x] desk.project.syncKnowledge — external KB file sync (issue #7)
- [x] desk.project.importSnapshot — session snapshot import (issue #8)
- [x] desk.project.exportToProject — export space content to fusion-projects (issue #9)

### V0.8.1 ✅ (Issue #3/#4 — Artifact Permissions + FSB Integration)
- [x] SpaceArtifactService — Artifact CRUD + ownership tracking + permission checks (issue #3)
- [x] Permission matrix — 15 actions (4 new: view/edit/share/transfer_artifact)
- [x] DeskRPC — 7 new desk.space.artifact.* handlers
- [x] ModuleRegistry — sidebar module registration (register/list/enable/disable) (issue #4)
- [x] NotificationService — approval task notification push (SSE + desk.notification.push) (issue #4)
- [x] DeskRPC — 4 desk.module.* + 3 desk.notification.* handlers
- [x] Store migration — space_artifacts columns + 2 new tables (sidebar_modules, space_notifications)
- [x] 29 new tests (18 artifact + 11 FSB, 519 total)

### Patch Releases

#### V0.2.12 (Patch) — 服务可用性审计修复 (Studio 集成)
- [x] **节点注册缺口**: 新增 `nodes/__init__.py::import_all_nodes()` 显式导入 9 个节点模块 (触发 `@register_node`); `DeskRPCServer.start()` + `desk rpc` CLI 调用之。历史 bug: 服务端仅 cli.py 副作用导入的 macos+browser 注册, `desk.nodes.list` 仅 33/47 节点可见 → 现 47/47
- [x] **节点列表字段不全**: `desk_rpc._handle_nodes_list` 委托 `NodeRegistry.list()` 返回 7 字段 (`name/display_name/category/description/icon/default_label/params_schema`); 原 3 字段取自 `__doc__`, Studio 无法渲染参数表单
- [x] **HTTP 通道默认未启**: `desk rpc` CLI 新增 `--http-port` (默认 11438), 默认并发启动 HTTP (`/rpc /health /mcp /sse`), 承载 Studio PluginBridge `plugins/*` 集成面板; `--http-port 0` 可禁用
- [x] **HTTP 版本漂移**: `mcp_http.py` `SERVER_VERSION` 硬编码 "0.1.0" → 动态读取 `__version__` (`/health` 返回真实版本)
- [x] **start.sh venv 指向错误**: 硬编码 `${PROJ_DIR}/.venv` (stale editable install `fusion_cowork-0.1.3`, 缺 plugins) → 优先 monorepo 根 `.venv` (`/Users/dahai/fusion/.venv`, 含 cowork 0.2.12 + plugins 0.3.3), 仅独立部署时回退本地。修复 `/rpc plugins.*` 一律 -32603
- [x] **doctor 健康检查增强**: 新增 HTTP 11438/health 探活 + fusion-plugins-ecosystem 可用性检查 (缺则明确提示安装命令)
- [x] 测试: 全套 593 passed (Py3.14), ruff 0 issues; UDS `desk.nodes.list` 实测 47 节点/7 字段, HTTP `/rpc plugins.ping` 实测 `{pong:true}`

#### V0.2.11 (Patch) — /rpc 端点托管 + plugins/* 委托 (#48)
- [x] 新增 `server/rpc_bridge.py` — 托管 `/rpc` JSON-RPC 端点, 委托 15 个 `plugins/*` 方法给 fusion-plugins-ecosystem `MCPHandler`
- [x] `mcp_http.py` 新增 `/rpc` 路由 (POST), 仅路由 `plugins/*`, 其余返回 -32601
- [x] `DeskRuntime` 注入 `NodeRegistry` + `TaskScheduler` + `FusionMLXClient`, Studio 可发现 cowork 节点
- [x] 依赖缺失降级: `fusion-plugins-ecosystem` 未安装时 `/rpc` 返回 -32603 + 安装提示 (新增 `[plugins]` extra)
- [x] Bug fix: `desk_rpc._handle_health` 硬编码 `version: "0.3.0"` → 动态读取 `__version__` (版本漂移修复)
- [x] Bug fix: `mcp_http.py` 移除 `from __future__ import annotations` — FastAPI 0.139 下 `Request` 注解字符串化导致路由 422
- [x] Bug fix: `mcp_http.py` `MCPToolRegistry` 改运行时导入 — Py3.11 下 `TYPE_CHECKING` 注解立即求值触发 `NameError` (Py3.14 注解延迟默认故本地未现, CI 3.11 暴露)
- [x] 降级语义: `/rpc` 非 plugins 方法 → -32601 (与依赖无关); plugins 方法依赖缺失 → -32603
- [x] 测试: 14 个 `/rpc` + `rpc_bridge` 用例, `skipif` 降级保护 CI; Py3.11 588 passed/5 skipped, Py3.14 593 passed, ruff 0 issues

#### V0.2.9 (Patch) — 商用问题修复
- [x] P0: Computer Use 循环截图以 `image_url` 多模态格式传入模型 (原盲调用, 致盲修复)
- [x] P0: `ScreenCaptureNode` 视觉分析复用同一多模态通道
- [x] P1: `_applescript_move` 缺 cliclick 时明确告警 (替代静默失败)
- [x] P1: CDP `list_network_requests`/`list_console_messages` 落地后台 reader loop 真实事件缓冲 (原 `return []` 桩)
- [x] P2: 文档计数漂移修正 (节点 33→47 / 6→7 分类, MCP 15→16, Hook 11→14, SpaceAPI 18→25)
- [x] 测试: 2 个 P0 多模态回归用例, 全套 579 passed, ruff 0 issues

#### V0.2.10 (Patch) — 鉴权对接文档修正 (#46)
- [x] 修正 `FUSION_MLX_API_KEY` 说明: 应为 fusion-gateway client key, 非 mlx backend key (两套独立鉴权)
- [x] README/README_CN 新增双鉴权说明 + 401 排错指引
- [x] `FusionMLXClient.chat()` 401/403 错误增加可操作提示 (引导检查 key 来源)
- [x] 测试: 579 passed, ruff 0 issues

#### V0.1.9 (Patch)
- [x] Remove local-only docs from git tracking (6 files → .gitignore)
- [x] feat: ast_diff module migrated from fusion-multi-node (#26, #27)

#### V0.1.8 (Patch)
- [x] Port standardization: 9760→11437, 9761→11438, 9762→11439 (#24, #25)
- [x] Ruff lint: 4 unused variable fixes in desk_rpc handlers

#### V0.1.7 (Patch)
- [x] 19 missing RPC handlers for Studio GUI (#19, #23)
- [x] P0: desk.space.chat.stream — streaming chat
- [x] P1: agent.update, snapshot CRUD (5), comment CRUD (2)
- [x] P2: workflow (3), discovery.scan, desktop (2), session snapshot (5)
- [x] Store: update_agent, remove_agent, snapshot get/delete, comment CRUD
- [x] Total RPC methods: 79 → 99

#### V0.1.6 (Patch)
- [x] RPC alias: `desk.space.chat.history` → chat.list (Studio compat, #20)
- [x] RPC alias: `desk.space.notification.*` → notification.* (Studio compat, #21)
- [x] GUI gap audit: 19 missing RPC handlers documented (#19)

#### V0.1.5 (Patch)
- [x] start.sh lifecycle manager (start/stop/restart/status/log/doctor/clean)
- [x] `python -m fusion_cowork` now works (__main__.py)
- [x] Remove desk.mlx.start/stop — product layer should not manage infra (#16)
- [x] Fix pyproject.toml description (#16)

#### V0.1.4 (Patch)
- [x] CI: GitHub Actions workflow (ruff lint + pytest + coverage)
- [x] Lint: ruff configured, F-series bugs fixed, 0 issues remaining
- [x] Test: 519 tests passing
- [x] Docs: environment variables section, CI badge

### V0.8 (Planned)
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

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

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
