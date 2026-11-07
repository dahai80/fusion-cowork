---
doc_id: fusion-cowork/guide/contributing
product: fusion-cowork
class: platform
entry: service
doc_type: guide
title: {zh: "贡献指南", en: "Contributing guide"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - CONTRIBUTING.md
  - AGENTS.md
---

# Contributing / 贡献指南

## 开发环境 / Development setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # or the monorepo root .venv
pip install -e ".[dev]"
./start.sh doctor
```

## 常用命令 / Common commands

```bash
pytest tests/                     # full suite (asyncio_mode=auto)
pytest tests/test_foo.py -v       # single file
ruff check . && ruff format .     # lint + format
./start.sh start && ./start.sh log -f
```

## 约定 / Conventions

- Python 3.11+, async-first, Apache-2.0. Code style: 4-space indentation, no docstrings, English-only comments and log messages.
- Process titles are mandatory: every long-running entrypoint sets `fusion-cowork <role> [:port]` via setproctitle (see AGENTS.md).
- Ports must be registered in `architecture/port-registry.yaml` before binding (run `python scripts/check-ports.py`).
- Every capability claim in engine code carries a `CLAIM: verified by tests/<file>.py` tag; `scripts/check_claims.py` fails CI on dangling refs.

## 提交流程 / Workflow

1. File or pick an issue at github.com/dahai80/fusion-cowork/issues.
2. Branch from `main`, implement within this repository only.
3. Run `pytest tests/` and `ruff check .` locally; all failures must be fixed, including pre-existing ones exposed by your change.
4. Open a PR in English with the issue reference; CI (`.github/workflows/ci.yml`) must pass.
