---
doc_id: fusion-cowork/get-started
product: fusion-cowork
class: platform
entry: service
doc_type: get-started
title: {zh: "快速上手", en: "Get started"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - pyproject.toml
  - docs/guides/guide.md
---

# 快速上手 / Get Started

## 安装 / Install

要求 / Requires: Python 3.11+，macOS（Apple Silicon）。

```bash
git clone https://github.com/dahai80/fusion-cowork.git
cd fusion-cowork
pip install -e ".[web,plugins,office]"
```

> 在 fusion monorepo 内 `.[plugins]` 会解析同仓的 fusion-plugins-ecosystem 与
> fusion-harness；独立克隆时改用 `pip install fusion-harness`（PyPI）。
> Inside the fusion monorepo `.[plugins]` resolves the sibling packages; in a
> standalone clone install the kernel from PyPI instead.

## 启动服务 / Start the services

```bash
./start.sh start      # desk RPC daemon (UDS /tmp/fusion-cowork.sock, HTTP :11438)
./start.sh status     # PID, socket, memory, uptime
./start.sh doctor     # venv / CLI / socket / upstream health
```

Web 画布（可选）/ Web canvas (optional):

```bash
fusion-cowork web serve --port 11461    # REST + SSE + web/dist, registry: fusion-cowork-web
```

可选 extras：`[web]` FastAPI/uvicorn，`[office]` xlsx/pptx 节点，`[cloud]` Postgres/JWT/k8s，`[test]` pytest。Optional extras: `[web]` FastAPI/uvicorn, `[office]` xlsx/pptx nodes, `[cloud]` Postgres/JWT/k8s, `[test]` pytest.

## 第一个工作流 / First workflow

```bash
fusion-cowork template list                       # built-in template library
fusion-cowork template show desktop_daily_cleanup
fusion-cowork template run desktop_daily_cleanup --dry-run
fusion-cowork workflow list
```

自然语言生成 / Natural-language generation (needs fusion-mlx on :11434):

```bash
fusion-cowork ai generate "把下载文件夹里一周内的 PDF 归档到 ~/Documents/PDF"
```

## 调度与技能 / Scheduling and skills

```bash
fusion-cowork schedule add --name "每日清理" --cron "0 9 * * *" --template desktop_daily_cleanup
fusion-cowork schedule list
fusion-cowork skill list            # markdown skill packs
fusion-cowork skill run-package contract-review
```

## 验证 / Verify

1. `./start.sh status` shows a live PID and socket.
2. `curl http://127.0.0.1:11438/health` returns 200.
3. `fusion-cowork system info` prints runtime, node count and budget.

## 下一步 / Next steps

- API 细节 / API details: [api-reference.md](api-reference.md)
- 配置 / Configuration: [config-reference.md](config-reference.md)
- 场景演练 / Scenario walkthroughs: [guides/guide.md](guides/guide.md)
- 排错 / Troubleshooting: [troubleshooting.md](troubleshooting.md)
