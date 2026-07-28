# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fusion-Desk is a local-first, zero-code desktop automation platform for macOS Apple Silicon. Part of the Fusion-MLX ecosystem (alongside Fusion-Code, Agent-Studio, Fusion-KB, Model-Hub). Python 3.11+, async-first architecture.

## Common Commands

```bash
# Setup
source .venv/bin/activate
pip install -e ".[test]"       # with test deps
pip install -e ".[web]"        # with FastAPI/uvicorn

# Test
pytest tests/ -v                                    # all tests
pytest tests/test_engine.py::TestWorkflowEngine -v   # single class
pytest tests/test_engine.py::TestTypeCoercion -v     # single test class
pytest tests/ --cov=fusion_desk --cov-report=html    # with coverage

# CLI
fusion-desk template list
fusion-desk template run <id> --dry-run
fusion-desk ai generate "prompt"
fusion-desk ai status
fusion-desk system info

# AI service (fusion-mlx)
~/claude-home/fusion-mlx/start.sh start|stop

# Embedded browser (Swift WKWebView)
fusion-desk browser build   # compile Swift
fusion-desk browser start   # launch .app
```

## Architecture

### Layered Design

1. **UI Layer** — CLI (click), Web UI (FastAPI), macOS .app
2. **Workflow Engine** — DAG-based (n8n-inspired): `WorkflowEngine` → topological sort → node execution → data passing
3. **AI Capability** — `FusionMLXClient` (HTTP → fusion-mlx port 8000), `NLWorkflowGenerator` (NL → workflow), `KBClient` (HTTP → Fusion-KB port 11434)
4. **System Capability** — macOS nodes via AppleScript/osascript + Python pathlib/shutil

### Core Engine (`fusion_desk/engine/`)

- **`node.py`** — `BaseNode` (abstract), `NodeRegistry` (class-level registry), `@register_node` decorator, type coercion (`_coerce_int/bool/number/array`) for LLM-generated string params
- **`workflow.py`** — `Workflow` (DAG container with cycle detection + topological sort), `WorkflowEngine` (async executor with progress callbacks, cancel, retry)
- **`scheduler.py`** — `TaskScheduler` (APScheduler-based cron)
- **`enhanced_scheduler.py`** — V0.2: calendar view, task dependency, stats
- **`optimizer.py`** — V0.2: AI workflow analysis, bottleneck detection

### Node System (`fusion_desk/nodes/`)

All nodes subclass `BaseNode`, use `@register_node`, and live in category subpackages:

| Subpackage | Category | Key Nodes |
|---|---|---|
| `nodes/macos/` | `macos_system` | DesktopClean, DownloadOrganizer, DiskCleaner, FileWatcher, ScreenCapture, Clipboard, Notification, AppLifecycle, OCR |
| `nodes/ai/` | `ai_processing` | AIClassify, AISummarize, AIGenerateName |
| `nodes/tools/` | `tool` | ShellExec, PythonREPL, WebSearch, FetchURL, ApplyEdit |
| `nodes/browser/` | `browser` | BrowserOpen, BrowserExtract, BrowserAutomate, BrowserClient, BrowserManager |
| `nodes/io/` | `io` | FileInput, FileOutput |
| `nodes/logic/` | `logic` | Filter, Loop, Merge |

### Key Patterns

- **Lazy Import** (`fusion_desk/__init__.py`): `__getattr__`-based lazy loading via `_LAZY_IMPORTS` dict. Keeps `import fusion_desk` fast; modules load on first attribute access.
- **Node Name Aliases** (`NODE_NAME_ALIASES`): Chinese user-friendly names → backend node names. Registered via `NodeRegistry.register_alias()`. Used by NL workflow generator.
- **Type Coercion**: `_coerce_*` functions convert LLM string outputs ("10", "true") to declared JSON Schema types. Applied automatically in `NodeRegistry.create()`.
- **Workflow Serialization**: `Workflow.to_dict()/from_dict()` supports both dict and list node formats. Edges define data flow between nodes.

### Other Modules

- **`fusion_desk/ai/`** — `FusionMLXClient` (httpx async), `KBClient`, `NLWorkflowGenerator`
- **`fusion_desk/templates/`** — `TemplateManager` (10 built-in + 10 industry templates), `industry_templates.py`
- **`fusion_desk/report/`** — `ReportGenerator` (Markdown/HTML batch reports)
- **`fusion_desk/orchestrator/`** — V0.3: `AgentOrchestrator` multi-agent orchestration
- **`fusion_desk/server/`** — `MCPServer` (15 tools for Claude Desktop/Code), `CrossDeviceSync` (WebSocket)
- **`fusion_desk/cli.py`** — Click CLI, instantiates engine/scheduler/template-mgr/AI-client as module globals
- **`fusion_desk/utils/logger.py`** — `setup_logger()` used by CLI

### Embedded Browser (`browser/`)

Swift Package (Package.swift) — WKWebView-based macOS .app with `fusion://` custom protocol. Built/launched via `BrowserManager` in `nodes/browser/`.

### Test Conventions

- Single test file: `tests/test_engine.py` — covers registry, workflow engine, type coercion, file IO, logic nodes, macOS nodes, AI client, tool nodes, aliases, lazy import
- All node modules must be imported in tests to trigger `@register_node` registration
- Mock nodes (`MockSuccessNode`, `MockFailNode`, `MockTransformNode`) defined inline in test file
- `pytest-asyncio` with `asyncio_mode = "auto"`
- Registry state must be saved/restored in tests that call `NodeRegistry.clear()`
