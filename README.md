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
  <img src="https://img.shields.io/badge/version-0.5.4-blue" alt="Version">
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

> **New to fusion-cowork?** Follow the **[Getting Started Guide](docs/guide.md)** — scenario-based walkthroughs (tidy your Desktop, AI-generate a workflow, batch docs, automate a site, schedule tasks, Claude Code MCP, collaboration, cloud deploy) with a decision table and troubleshooting tree. This README is the full command/node reference catalog; the guide is the on-ramp.

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
| `FUSION_MLX_URL` | `http://localhost:11432/v1` | fusion-mlx base URL — full URL, highest priority (overrides host/port) |
| `FUSION_MLX_HOST` | `localhost` | fusion-mlx host (composed with `FUSION_MLX_PORT`; used only when `FUSION_MLX_URL` unset) |
| `FUSION_MLX_PORT` | `11432` | fusion-mlx port (composed with `FUSION_MLX_HOST`; used only when `FUSION_MLX_URL` unset) |
| `FUSION_MLX_MODEL` | _(unset)_ | default chat model for NL workflow generation (`NLWorkflowGenerator`). Pin a chat-capable model id to avoid picking `image_gen`/`video_gen` models (issue #85) |
| `FUSION_RAG_URL` | `http://localhost:11436` | fusion-rag (fusion-kb) base URL |
| `FUSION_IDENTITY_ENABLED` | _(unset)_ | opt-in: set `1` to activate fusion-identity as sole JWT issuer + tenant registry (issue #88). Default OFF = zero behavior change (local JWT/static-token dev path unchanged) |
| `FUSION_IDENTITY_URL` | `http://127.0.0.1:11470` | fusion-identity service base URL (only used when `FUSION_IDENTITY_ENABLED=1`) |
| `FUSION_IDENTITY_SERVICE_TOKEN` | _(unset)_ | service token sent as `Authorization: Bearer` to fusion-identity `/verify`. Required when enabled (else identity client stays None) |

> **base_url 优先级** (issue #83): `FUSION_MLX_URL` (整 URL) > `FUSION_MLX_HOST` + `FUSION_MLX_PORT` > 默认 `localhost:11432`。默认不变 → 多节点/gateway 部署字节级无影响。**本地单机直连 mlx** (绕过 gateway, 用 mlx key 而非 gateway key):
> ```bash
> export FUSION_MLX_HOST=127.0.0.1 FUSION_MLX_PORT=11434
> ```
>
> **鉴权说明**: fusion-cowork 通过 fusion-gateway(:11432) 调 fusion-mlx,存在**两套独立鉴权**:
> 1. **gateway client key** — 客户端请求 gateway 时用,即本变量 `FUSION_MLX_API_KEY`(取自 gateway `config.yaml` 的 `auth.api_keys[].key`)
> 2. **mlx backend key** — gateway 转发到 mlx 时用,配置在 gateway `config.yaml` 的 `backends.fusion-mlx.api_key`,客户端无需关心
>
> 常见 401 错误:把 mlx 的 key 填进了 `FUSION_MLX_API_KEY`。应填 gateway 的 client key。本地单机直连 (上面 `FUSION_MLX_PORT=11434`) 时则相反 — 此时 `FUSION_MLX_API_KEY` 应填 mlx 的 key。

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

# fusion-guard integration (issue #73, opt-in, default OFF)
# Set FUSION_GUARD_ENABLED=1 + ensure /tmp/fusion-guard.sock exists to activate.
# HIGH_RISK_NODES delegate to guard.evaluate (UDS JSON-RPC); low-risk nodes stay local.
# Guard unreachable -> cached rules fail-closed (deny high-risk). Optional shared secret
# via FUSION_GUARD_SHARED_SECRET. Confirm pending L3 approvals via desk.permission.confirm_guard.

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

# fusion-browser CDP shim target (issue #65, issue #77)
# FUSION_BROWSER_CDP=<port> -> drive the embedded fusion-browser shim on 127.0.0.1:<port> instead of external Chrome.
# The shim hardens its WS upgrade with a fail-closed Origin gate (E-15): the client MUST send an
# allowlisted Origin. Set FUSION_CDP_ORIGIN=<origin> to match the operator's allowedOrigins config
# (e.g. https://fusion.local). Without it the WS upgrade, Page.navigate and PUT /json/new are denied.
export FUSION_BROWSER_CDP=9223
export FUSION_CDP_ORIGIN=https://fusion.local

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

# Mobile push notification (Bark/ntfy/本地降级) — V0.2.13
fusion-cowork push send "任务完成" "桌面清理已完成 12 个文件"
fusion-cowork push config --bark-url https://api.day.app --ntfy-url https://ntfy.sh

# Deep Research (多 Agent 研究) — V0.2.13
fusion-cowork research run "对比 MLX 与 llama.cpp 在 Apple Silicon 上的推理性能" --depth 3 --max-sources 3

# UltraReview (多 Agent 代码审查) — V0.2.13
fusion-cowork review run fusion_cowork/engine/workflow.py --lens security --lens correctness
fusion-cowork review run --diff          # 审查 git diff 变更集

# LSP 代码智能 (定义/引用/悬停) — V0.2.13
fusion-cowork lsp definition fusion_cowork/engine/workflow.py:120
fusion-cowork lsp references fusion_cowork/engine/node.py NodeRegistry
fusion-cowork lsp hover fusion_cowork/ai/mlx_client.py:45

# Worktree (git 隔离执行) — V0.2.13
fusion-cowork template run <id> --worktree

# MCP 导入 (add-from-Claude-Desktop) — V0.2.13
fusion-cowork mcp import ~/Library/Application\ Support/Claude/claude_desktop_config.json

# 远程控制 TLS + 命名会话 attach — V0.2.13
fusion-cowork remote serve --port 11439 --token mytoken --tls --tls-cert cert.pem --tls-key key.pem
fusion-cowork remote attach my-session --url wss://host:11439/control --token mytoken

# 技能持久化 + 用户自写 skill 包 — V0.2.13
fusion-cowork skill save /my-cleanup ~/skills/my-cleanup.json
fusion-cowork skill load ~/skills/my-cleanup.json

# 交互式对话 Agent Loop (白话→自主拆解多步→观察→决策→行动) — V0.2.14
fusion-cowork agent-loop run "整理下载目录里近 7 天的截图归档到桌面"
fusion-cowork agent-loop chat            # REPL 模式, 中途可 stop 叫停 / 补一句再续

# 实时光标 + 成员 presence/online_status — V0.2.14
curl -X POST localhost:8000/spaces/s1/presence/heartbeat -d '{"user_id":"u1"}'
curl -X POST localhost:8000/spaces/s1/presence/cursor -d '{"user_id":"u1","x":120,"y":80,"target":"art1"}'
curl localhost:8000/spaces/s1/presence

# 协作层 WebSocket 双向 (聊天/光标/presence 实时广播) — V0.2.14
fusion-cowork collab serve --port 11439
# 或 FastAPI WS 端点: ws://host:8000/spaces/{space_id}/ws (hello: {"user_id":..,"display_name":..})
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
│   (DAG, n8n-inspired) │  (APScheduler) │  (67 nodes)   │
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

### Node Types (67 nodes built-in, 7 categories)

| Category | Count | Nodes |
|----------|-------|-------|
| `tool` | 36 | Shell Exec, Python REPL, Web Search, Fetch URL, Apply Edit, Browser Open/Extract/Automate, **CDP** (Navigate, Snapshot, Click, Fill, Fill Form, Screenshot, Evaluate, Emulate, Network, Console, Drag, Hover, Press Key, Type Text, Upload File, Wait For, Handle Dialog, Resize Page, Heap Snapshot, Lighthouse, List Pages, New Page, Select Page, Close Page, Performance Trace), **Push, LSP, Worktree** |
| `macos_system` | 13 | Desktop Clean, Download Organizer, Disk Cleaner, File Watcher, Screen Capture, Clipboard, Notification, App Lifecycle, OCR, Mouse Move, Mouse Click, Keyboard Type, Keyboard Shortcut |
| `file_operation` | 6 | File Classifier, Batch Rename, Copy, Move, Delete, Find |
| `ai_processing` | 4 | AI Classify, AI Summarize, AI Generate Name, AI Vision Analyze |
| `logic` | 3 | Filter, Loop, Merge |
| `fusion_ecosystem` | 3 | Trainer (Fusion-Trainer 互通), **Memory Commit, Memory Retrieve** (Fusion-Memory 互通) |
| `io` | 2 | File Input, File Output |

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

## ☁️ Multi-tenant Cloud SaaS (v0.4.0)

Fusion-Cowork v0.4.0 extends the local-first desktop platform into a **multi-tenant cloud SaaS** — same engine, cloud-ready isolation/auth/observability/deploy. All cloud features are **opt-in** (env/config set → activate; unset → unchanged local behavior).

### Tenant Isolation

- **Row-level `tenant_id`** column on all 14 store tables + query guards (`WHERE tenant_id = ?`) on every CRUD path
- **Postgres RLS** as defense-in-depth (`CREATE POLICY tenant_isolation USING (tenant_id = current_setting('app.tenant_id'))`)
- **Single schema**, single `SpaceStore` class with backend seam: pass `dsn`/`FUSION_PG_DSN` → Postgres (asyncpg pool); pass `data_dir` → SQLite (tests/local)
- 9 cross-tenant IDOR points sealed (delete_message, list_comments, get_invite, list_spaces, notifications, sessions, space CRUD, messages)

### Authentication (JWT)

- **PyJWT** verifies externally-issued **HS256/RS256** tokens, extracts `tenant_id`/`user_id`/`scopes` claims → `TenantPrincipal`
- **Static token fallback** retained for local/desktop (`auth.static_token`); production enforces JWT (`FUSION_REQUIRE_JWT=1`)
- JWKS remote fetch for RS256 (optional cache); failures log without leaking token plaintext
- ConfigCenter secrets **redacted in logs** + **encrypted at rest** (Fernet, `FUSION_ENCRYPTION_KEY`)

### Persistence & Migrations

- **SQLite → PostgreSQL** migration (asyncpg connection pool, MVCC)
- **Versioned migrations** (`schema_migrations` table, `MigrationRunner` — ordered apply, rollback on failure)
- **Backup/restore** via `pg_dump` wrapper — `fusion-cowork db backup|restore`
- Placeholder normalization helper (`?` → `$1,$2,...`) keeps 30 SQL sites backend-agnostic

### Observability

- **Structured logging** (structlog JSON, `setup_logger(json=True)`) + trace_id contextvar propagating across `await`
- **OpenTelemetry** metrics (`fusion_requests_total`, `fusion_request_duration_seconds`, `fusion_active_connections`, `fusion_db_errors_total`) + tracing (OTLP exporter) + prometheus `/metrics`
- **Deep `/health`** — checks DB (`SELECT 1`), disk threshold, upstream MLX/KB reachability → `{status, checks}`
- **SIGTERM graceful drain** — stop accepting, drain in-flight, close store/pool

### Security at Scale

- **fusion-guard integration** (issue #73) — `HIGH_RISK_NODES` delegate authorization to `guard.evaluate` via UDS JSON-RPC (`/tmp/fusion-guard.sock`); low-risk nodes stay local (no per-node IPC). Opt-in: `FUSION_GUARD_ENABLED=1` + socket present. `CONFIRM`/L3 → `guard.confirm` (pending store); L4 → block. Guard unreachable → cached rules (`guard.rules.dump` → `~/.fusion-guard/rules-cache.json`) fail-closed (deny high-risk), not fail-open. Optional `FUSION_GUARD_SHARED_SECRET`. Default OFF — zero behavior change.
- **CDP Origin allowlisting** (issue #77) — fusion-browser's CDP-over-WS shim hardened its Origin gate to fail-closed (E-15): an empty `Origin` is rejected, an empty `allowedOrigins` denies everything except local schemes (`data:`/`about:`/`blob:`). The CDP client sends an allowlisted `Origin` header on the WS upgrade via `FUSION_CDP_ORIGIN` (must match the operator's `allowedOrigins` config). Clears Bearer (H-5, issue #72) but does not clear Origin (E-15) — the two gates are independent.
- **Per-tenant rate limiting** (token bucket, `FUSION_RATE_LIMIT_*`)
- **Per-tenant quotas** (`TenantQuotas`: max_spaces/messages/artifacts/agents/storage, default unlimited)
- **Tamper-evident audit log** (sha256 chained `prev_hash`, `verify_chain`)
- **Upstream circuit breaker** (MLX/KB — `failure_threshold`/`recovery_timeout`, degrade instead of hang)
- **WS connection caps** (global + per-tenant) + collab session idle eviction (ResourceWarning leak fix)
- **Config secrets encrypted at rest**

### Deployment

- **Dockerfile** (multi-stage, nonroot, HEALTHCHECK) + **docker-compose** (app + postgres:16)
- **Helm chart** (`deploy/helm/fusion-cowork/`): Deployment (liveness/readiness probes, `terminationGracePeriodSeconds`, preStop), Service, Secret (JWT/PG DSN), ConfigMap, HPA (CPU 70%, 2–10 replicas)
- **`--container` flag**: binds `0.0.0.0`, skips UDS/venv detection — container-friendly
- `FUSION_CONFIG_DIR` env overrides config location (volume mount)

### Plugin Ecosystem Hardening

- **Ed25519 manifest signing** (`plugins/signing.py`) — `signature` field, canonical bytes, `verify_any_key` against configured public keys, `require_signing` opt-in
- **Plugin registry** (`plugins/registry.py`) — persistent `registry.json`, **downgrade rejection** (version tuple compare), checksum
- Sandbox isolation unchanged (process-out, rlimit) — signing is additive

### Install Cloud Extras

```bash
pip install -e ".[cloud,web]"   # pyjwt, asyncpg, cryptography, prometheus-client, opentelemetry, structlog
# dev:
pip install -e ".[dev]"         # test+web+plugins+cloud
```

### CI

5-job matrix: `lint` (ruff check + format) · `test-sqlite` (3.11/3.12/3.13 × ubuntu/macos) · `test-postgres` (postgres:16 service) · `test-slow` (load/E2E/chaos) · `security` (pip-audit). Coverage gate + SBOM workflow (CycloneDX + pip-audit artifact).

## 🌐 Distributed State Layer (v0.5.0)

Opt-in cross-node shared state for multi-node / macOS MLX cluster deployments (issue #79). **Default OFF** — zero behavior change when unset.

- **`DistributedStateStore`** (`fusion_cowork/distributed_state.py`) — atomic file-backed (JSON) cross-process store. Thread + coroutine safe (`threading.Lock` + `asyncio.Lock`), atomic `temp`+`os.replace` write, corrupt-file rebuild. Serializes: cluster node registry, vRAM allocation ledger, plugin enable/installed sets.
- **Cluster-aware handle wrappers** — `ClusterNodeRegistry` merges local `NodeRegistry` + peer nodes from the shared store; `ClusterTaskScheduler` adds `dispatch_with_failover()` (best-node selection by free vRAM + tags, cycles candidates on failure). Injected into `DeskRuntime` when cluster enabled.
- **vRAM ledger** — `record_vram_allocation` / `can_allocate_vram(limit_mb)` enforce a cluster-wide budget so N nodes loading MLX models can't collectively exceed physical unified memory (prevents swap avalanche).
- **Plugin-state sync** — `record_plugin_state` / `is_plugin_enabled_anywhere` make a plugin enabled on node A visible to node B's `plugins/states`.

Enable:

```bash
export FUSION_CLUSTER_ENABLED=1
export FUSION_CLUSTER_NODE_ID=node-a        # unique per node
export FUSION_CLUSTER_STATE_PATH=/shared/cluster-state.json  # shared volume / NFS
```

> **Note:** `DeskRuntime`'s *internal* state (`vram_allocations` / `_mcp_sessions` / `registered_plugin_ids`) is unreachable through the injected handles — that consumer-side serialization is tracked upstream at [fusion-plugins-ecosystem#13](https://github.com/dahai80/fusion-plugins-ecosystem/issues/13). This layer provides the shared store + handle wrappers the consumer will be wired into.


---

## 🔐 fusion-identity Integration (v0.5.3)

Retire the local JWT/tenant/RBAC/quota reimplementation in favor of **fusion-identity** as the sole JWT issuer + tenant registry for the ecosystem (multi-tenant PRD §3/§4, issue #88). **Default OFF** — opt-in, matches the guard (#73) / cluster (#79) pattern; zero behavior change when unset, all existing tests green.

- **`IdentityClient`** (`fusion_cowork/auth/identity.py`) — sync `httpx.Client` `POST /api/v1/auth/verify` (header `Authorization: Bearer <FUSION_IDENTITY_SERVICE_TOKEN>`, body `{"token": "<user JWT>"}`). jti→claims cache (TTL 60s, cap 1024) cuts repeat calls; `revoked`/`tenant_status != active`/conn-fail → fail-closed (no silent fallback to local JWT in prod mode). `emit_usage()` → `POST /api/v1/tenants/{tid}/usage` (best-effort).
- **Seamless delegation** — `get_default_verifier()` (jwt.py) returns an `_IdentityJWTAdapter` when enabled, so every consumer (mcp_http `_auth_denied`, space/api, rate_limit) gets identity-backed verify with no call-site change. `verify_any_token` (fallback.py) gates static-token dev fallback: enabled + `FUSION_REQUIRE_JWT=1` + verify failed → fail-closed (no static fallback in prod). WS/TCP paths (`remote.py`/`sync.py`/`collab_ws.py`) covered by the same seam.
- **UDS `desk_rpc`** (non-FastAPI, no middleware) — `_authenticate` calls `IdentityClient.verify()` directly (Step 0), fail-closed on revoked/unreachable, keeps existing `set_current_tenant`.
- **FastAPI apps** (space/api, mcp_http ×2) — adopt `fusion_core.tenant.install_tenant_middleware` (enforces `X-Tenant-Id` header + jwt.tid↔header match) + a cowork bridge middleware that propagates `fusion_core.tenant.TenantContext` → cowork's `get_current_tenant()` contextvar (the dual-contextvar crux: fusion_core sources+enforces, cowork bridge propagates — not two competing enforcers).
- **Quotas from identity** — `QuotaEnforcer(identity_client=...)` reads `VerifyResponse.quota` from the verify cache (replaces ConfigCenter); `record_usage()` emits to fusion-identity (best-effort).
- **Dev fallback retained** — disabled (or reachable + `FUSION_REQUIRE_JWT` unset) → existing `verify_static_token` / local `JWTVerifier` / ConfigCenter quotas unchanged (local-first single-machine dev).

Enable:

```bash
export FUSION_IDENTITY_ENABLED=1
export FUSION_IDENTITY_URL=http://127.0.0.1:11470
export FUSION_IDENTITY_SERVICE_TOKEN=<service-token>
# prod mode (no static fallback):
export FUSION_REQUIRE_JWT=1
```

> **Production contract & defaults:** see [`docs/identity.md`](docs/identity.md) — env defaults, deploy topology, and the HTTP (`Authorization: Bearer` + `X-Tenant-Id`) / UDS (`_auth_token` flat string) header contract fusion-studio and other clients should use.
>
> **Defense-in-depth:** all `fusion_core.tenant` / `install_tenant_middleware` imports live inside `is_identity_enabled()` guards (lazy at enable-time), mirroring `mlx_client.py`'s guarded-import pattern — if fusion-core is ever absent, the identity path silently no-ops rather than breaking import. Postgres RLS kept (unchanged). `fusion-core` / `fusion-identity` are in-tree monorepo packages (not on PyPI); install via `pip install -e fusion-core` / `-e fusion-identity` from the monorepo root, not via extras (CI does a single-repo checkout, so they are absent there — middleware tests skipif).

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
    "sandbox": true
}
```

```bash
# Install from directory or zip
fusion-cowork plugin install /path/to/plugin_dir
fusion-cowork plugin install /path/to/plugin.zip
```

**Plugin sandbox runtime isolation (v0.2.15, P1-6):** When `manifest.sandbox == true`, plugin code never runs in the main process. `PluginLoader._load_sandboxed()` spawns a `sandbox_runner` subprocess to introspect node metadata, registers `SandboxedNode` wrapper subclasses in-process, and delegates every `execute()` back to the subprocess (rlimit CPU/memory/NPROC/FSIZE + timeout). The main process never calls `importlib.exec_module` on the plugin. `sandbox == false` keeps the legacy in-process load path (backward compatible).

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
- [x] FusionMLXClient fusion-core 基础设施采纳 (路径B, issue #58) — 借 `fusion_core.http_client.with_retry`+连接池 (统一重试+指标上报+LRU池), 保自建客户端业务层, 18 调用方零改; 成功路径空 content D-H3 守卫 (`finish_reason=empty_content`); CI 无 fusion-core 时 fallback 手写重试
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

#### V0.3.1 — 架构债务审计修复 (10 CRITICAL A + 8 HIGH R + 15 E 工程缺陷全关闭)
架构审计 (`audit/fusion-cowork-audit-report-0825.md`, 判定 NOT PRODUCTION-READY, 10 放行门槛) 全修复, v0.3.0→v0.3.1。7 阶段独立验证 (每阶段 ruff 0 + pytest 绿 + checkpoint commit)。

- [x] **Stage 1 — 沙箱+权限+REPL+CDP/URL 安全纵深 (A 系列)**: scoped_folder 路径规范化统一 (resolve(strict=False)+expanduser); seatbelt profile 收紧; PythonREPL AST 拒危险调用深化; CDP JS eval allow_js 网关 + 表达式注入阻断; FetchURL SSRF 私网阻断 + redirect 收紧
- [x] **Stage 2 — 认证/授权四路绕过全堵 (R 系列)**: UDS 身份绑定 + 跨 handler principal 一致性; space IDOR 守卫全覆盖
- [x] **Stage 3 — 死配置接线 + MCP 映射/错误**: 死配置/未接线 feature 接通; MCP tools/call 映射校正; 错误信息脱敏
- [x] **Stage 4 — 并发正确性 (DB+类级锁+Hook)**: aiosqlite 写锁; NodeRegistry 类级 RLock 防 TOCTOU; HookManager 异步派发不阻塞事件循环
- [x] **Stage 5 — 资源管控 (泄漏+超时+僵尸)**: 子进程组 killpg 杜绝孤儿; executor wait_for 超时; 资源句柄 finally 清理
- [x] **Stage 6 — 持久化原子性+剩余工程缺陷**: E-1 trainer_node PATH 解析; E-2 AppleScript 转义; E-4 持久化原子写 (tmp+os.replace+fsync, enhanced_scheduler + ApplyEdit); E-7 DeskRPC engine 单例; E-15 schema 类级缓存 (免 47× 实例构造); R-4 断点续跑排除 running; R-5 调度失败计数熔断; R-5b cron 描述安全 int; R-6 WS 广播并发+慢客户端隔离+presence TTL GC; R-7 workflow_sync 旧版本拒收; R-8 MCP transport 有界读 8MiB
- [x] **Stage 7 — 版本 bump + 文档**: v0.3.0→v0.3.1 (pyproject.toml + `__init__.py`); README + CLAUDE.md 更新
- [x] 测试: 910 passed / 1 skipped, ruff 0 issues

#### V0.3.0 — 安全加固 (审计 74 项发现全修复)
对抗性审计 (`audit/fusion-cowork-0824.md`, 74 项: 23 CRITICAL / 20 HIGH / 18 MEDIUM / 13 LOW, 判定 NOT PRODUCTION-READY) 全修复, v0.2.15→v0.3.0。

- [x] **Stage 1 — 认证基线**: UDS socket 文件权限 0o600 + 可选 `desk.auth_token` 握手; `_authenticate()` 拒收 params 内 operator_id/user_id 身份字段 (仅信连接身份); MCP HTTP/streamable Bearer 中间件 (`mcp.auth_token`); remote TLS fail-closed (坏证书不降明文)
- [x] **Stage 2 — 输入净化**: ShellExec 改 `create_subprocess_exec(*shlex.split)` 消 shell 注入; nl_parser 逐节点校验 name ∈ NodeRegistry; PythonREPL 改 AST walk 拒危险调用; store update_space/update_member 列白名单; CDP navigate scheme 校验 (仅 http/https) + evaluate 进 HIGH_RISK; AppleScript 转义顺序修正
- [x] **Stage 3 — 权限模型 + 沙箱**: 新增 `PermissionLevel.CONFIRM` 默认 (check 重排: approve→allow / deny→deny / high-risk→deny / else→allow); HIGH_RISK_NODES 补全至 ~30 节点; ApplyEdit 加 `ensure_allowed`; register 拒覆盖内置名; sandbox=false 需 `plugins.trusted` 白名单; zip-slip 逐条 `relative_to` 校验; darwin seatbelt `sandbox-exec` 限制 fs/network + env 白名单 + setrlimit fail-closed + stdin 有界读 16MiB + traceback 不进 RPC
- [x] **Stage 4 — 并发/执行正确性**: execute() 深拷贝 nodes 避共享 BaseNode 写污染; recorder 按 execution_id 命名空间; ComputerUseLoop 动作白名单 + 高危确认; orchestrator 真保留 Task handle + cancel 真杀协程; ShellExecutor 超时 kill+wait; 各 executor `wait_for(timeout)` + finally 清理; 委托深度上限 (默认 5)
- [x] **Stage 5 — 错误处理/信息泄漏**: 错误帧带 trace_id, 栈仅日志不泄客户端; space/KB 输入校验 (必填/类型/长度); rpc_bridge params schema 校验 -32602
- [x] **Stage 6 — MEDIUM+LOW 收尾**: MessageBus Queue(1024)+deque(1000) drop; ConfigCenter 原子写 (tempfile+flock+0o600+replace) + RLock + 单例锁 + 观察者迭代快照; 插件 URL https-only + 重定向拒 + 50MiB + sha256; claude_desk command 校验; scoped_folder 单例锁 + symlink 拒; mcp_gateway spawn fail-closed (`FUSION_ENABLE_GATEWAY=1`); collab_ws auth_token + 成员校验; latent — desk.space.workflow.* 改走 SpaceArtifactService + Workflow.from_dict (原 AttributeError); JSON-RPC batch -32600 拒 + UDS 有界读 + uvicorn 并发/请求体上限
- [x] 测试: 868 passed / 1 skipped, ruff 0 issues

#### V0.2.15 (Patch) — 审计缺口补齐 Stage-1 + Stage-2 (文件沙箱 + 断点续跑 + 插件进程外隔离 + P2 测试补齐)
- [x] **P0-2 授权工作文件夹沙箱** — `security/scoped_folder.py` `ScopedFolderManager.ensure_allowed()` (resolve + relative_to 边界检查) 注入 6 个文件节点 (FileInput/Output/Copy/Move/Find/Delete + BatchRename) + ShellExec; 越界读/写拒绝或跳过
- [x] **P1-2 断点续跑** — `WorkflowEngine.execute(resume_steps=...)`: seed 已完成步骤 output_data → 跳过重跑 (SKIPPED); CLI `workflow/template run --resume <session_id>` 经 `SessionStore.resume()` 取快照; 修复 session steps_snapshot 丢 output_data bug; 3 回归测试
- [x] **P1-6 插件沙箱进程外隔离** — `sandbox=true` 插件走 `sandbox_runner` 子进程 introspect + `SandboxedNode` 包装子类 (`make_sandboxed_node_class` 动态生成), `execute()` 回子进程运行 (rlimit CPU/内存/NPROC/FSIZE + 超时); 主进程永不 `exec_module` 插件代码; 4 测试
- [x] **P2-9 MCP Streamable HTTP 测试补齐** — `tests/test_mcp_streamable.py` 覆盖 2025-03-26 spec 全链路: initialize 建 Mcp-Session-Id / notifications/initialized 202 / 未初始化拦截 (-32002) / tools/list / tools/call / Accept: text/event-stream → SSE 流 / DELETE 终止会话 / health; 12 测试
- [x] **P2-10 远程控制 TLS + attach 修复与测试** — 修复 `remote.py::_attach_session` 误调 `store.get_session()` (SessionStore 无此方法, 永抛异常 → attach 恒失败) → 改为 `store.get()`; 新增 TLS 测试 (无证书 None / 坏证书降级 None / openssl 自签名 → SSLContext TLSv1.2+) + attach 测试 (缺 session_id / 不存在 / 命中返回快照+绑定); 6 测试
- [x] 测试: 695 passed / 1 skipped (Py3.14), ruff 0 issues

#### V0.2.14 (Patch) — 协作实时能力补齐 (Agent Loop + presence + WS 双向)
- [x] **交互式对话 Agent Loop** — 新增 `agent_loop/` 包: LLM 逐轮决策 (RUN_NODE/REPLY/ASK/DONE) → NodeRegistry.create + resolve_alias 执行节点 → 观察结果回注入消息历史 → 循环至 DONE/max_steps; `interrupt()` 叫停 + `supplement()` 中途补一句再续; `agent-loop run/chat` CLI; 12 tests
- [x] **实时光标 + 成员 presence/online_status** — 新增 `space/presence.py`: 内存 `{space:{user:PresenceState}}`, 心跳时间戳判在线/离线 (60s 超时), 光标 (x,y,target) + EventEmitter 广播; REST 端点 (heartbeat/cursor/list/remove) + `desk.space.presence.*` RPC; 10 tests
- [x] **协作层 WebSocket 双向** — 新增 `server/collab_ws.py` `CollabHub`: 按 space_id 分房间, WS 连接绑定 (space_id,user_id); 入站 chat_send/cursor_move/presence/ping/leave → 出站广播 (排除发送者); 集成 SpaceChatService 持久化 + PresenceManager; FastAPI WS 端点 `/spaces/{id}/ws` + `desk.space.collab.*` RPC 轮询桥 + `collab serve` 独立 WS 服务; 13 tests
- [x] 测试: 668 passed / 1 skipped (Py3.14), ruff 0 issues

#### V0.2.13 (Patch) — Capability Parity Stage-3 (审计缺口补齐)
- [x] **P2-5 移动推送通知** — 新增 `notification/` 包: Bark / ntfy 双 provider + 本地 osascript 降级; `PushNode` 工作流节点 + `push send/config` CLI; ConfigCenter 持久化配置
- [x] **P2-6 Deep Research (多 Agent 研究)** — 新增 `research/` 包: LLM 规划分解子问题 → DuckDuckGo Lite 并行搜索 → 抓取摘要 → LLM 合成带引用报告; 全链路降级 (无 LLM → 原问题直搜 + 原始发现罗列); `research run` CLI
- [x] **P2-7 UltraReview (多 Agent 代码审查)** — 新增 `review/` 包: 多视角 (security/correctness/style/tests) 并行 LLM 审查 → 去重 + severity 排序 → LLM 合成报告; 支持 git diff 变更集审查; 全链路降级 (无 LLM → 文件清单); `review run` CLI
- [x] **P2-3 LSP 代码智能** — 定义/引用/悬停查询 (基于 tree-sitter / ripgrep 回退); `lsp` CLI 命令组
- [x] **P2-1 Worktree 隔离执行** — `template run --worktree` 在独立 git worktree 执行工作流, 避免污染工作区
- [x] **P2-8 add-from-Claude-Desktop MCP 导入** — 解析 Claude Desktop config.json, 导入 MCP server 配置; `mcp import` CLI
- [x] **P2-10 远程控制 TLS + 命名会话 attach** — `remote serve --tls` 加密通道 + `remote attach <name>` 命名会话重连
- [x] **P2-11 技能持久化 + 用户自写 skill 包** — `skill save/load` 持久化技能定义, 支持用户自定义 skill 包文件
- [x] **P1-1 上游 (issue/PR)** — fusion-studio Chat/Cowork 首页 + 授权文件夹选择 (已提 issue+PR 给 fusion-studio, 本地落地 code)
- [x] 测试: 633 passed / 1 skipped, ruff 0 issues

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
