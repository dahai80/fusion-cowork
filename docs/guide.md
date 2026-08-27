# Getting Started Guide — Scenario Walkthroughs

A guided, scenario-based path from zero to productive with fusion-cowork. Each scenario is a complete loop: a real situation → the commands to run → what you'll see → the result → how to verify → how to make it automatic → tweaks.

> Prefer a flat catalog of every command? See the [README](../README.md) (English) or [README_CN](../README_CN.md) (中文) instead. This guide is the on-ramp.

---

## Part 1 — Install & verify (do this once, ~2 minutes)

```bash
# Install (pick one)
pip install -e ".[web]"          # AI + web stack, recommended
# or: pip install -e .           # core only, no AI

# Verify the install is healthy — run all three
fusion-cowork system info        # version, platform, python → CLI on PATH
fusion-cowork ai status          # fusion-gateway(:11432) → fusion-mlx reachability
fusion-cowork template list      # 10 built-in templates → node registry loaded
```

**What success looks like:**

| Command | Green = | Red = |
|---------|---------|-------|
| `system info` | Version printed, no traceback | `command not found` → not installed or wrong venv |
| `ai status` | `reachable` / `ok` | `unreachable` → fusion-mlx not started, or wrong API key ([Part 4](#part-4--troubleshooting-decision-tree)) |
| `template list` | 10 templates listed | empty or only 33 → nodes not all imported |

> **No AI yet?** `ai status` unreachable is fine for scenarios 1 and 5 (no AI needed). It only blocks AI scenarios (2, 3). Start the engine when you get there: `~/claude-home/fusion-mlx/start.sh start`.

Healthy? Jump to Part 2 and pick a scenario.

---

## Part 2 — Pick your scenario

Find the outcome you want, click through. Don't read all of them.

| You want to... | Go to | Needs AI | Effort |
|----------------|-------|----------|--------|
| Tidy a messy Desktop / Downloads folder | [Scenario 1](#scenario-1--tidy-a-messy-desktop--downloads) | ❌ | 2 min |
| Type one sentence and let AI build the automation | [Scenario 2](#scenario-2--one-sentence--ai-builds-the-automation) | ✅ | 5 min |
| Batch-summarize / classify / rename a pile of documents | [Scenario 3](#scenario-3--batch-process-a-pile-of-documents) | ✅ | 5 min |
| Automate clicks and fills on a website | [Scenario 4](#scenario-4--automate-a-website-cdp) | ❌ | 5 min |
| Make a task run automatically every day | [Scenario 5](#scenario-5--make-a-task-run-automatically-every-day) | ❌ | 3 min |
| Let Claude Code (or any MCP client) drive your Mac | [Scenario 6](#scenario-6--let-claude-code-drive-your-mac-mcp) | optional | 5 min |
| Collaborate with a team / run multi-agent research | [Scenario 7](#scenario-7--team-collaboration--multi-agent-research) | ✅ | 8 min |
| Deploy fusion-cowork as a multi-tenant cloud service | [Scenario 8](#scenario-8--deploy-as-multi-tenant-cloud-saas) | optional | 10 min |

**Not sure?** Start with Scenario 1 — zero AI, zero config, you finish with a clean Desktop in 2 minutes.

---

## Part 3 — Scenario library

### Scenario 1 — Tidy a messy Desktop / Downloads

**Situation:** Your Desktop (or Downloads) is a pile of screenshots, PDFs, installers, zip files. You want it organized by type — and you want this to happen with one command, no AI, no setup.

**Step 1 — Inspect the ready-made workflow:**
```bash
fusion-cowork template show desktop_daily_cleanup
```
*You'll see:* the nodes that make up the workflow (file scan → classify by type → move into typed folders), and the params it takes.

**Step 2 — Preview before touching anything:**
```bash
fusion-cowork template run desktop_daily_cleanup --dry-run
```
*You'll see:* the execution graph + resolved params — exactly what would happen, no files moved yet. Read it: is it scanning the right folder? If not, note the param to change in Step 5.

**Step 3 — Run it for real:**
```bash
fusion-cowork template run desktop_daily_cleanup
```
*You'll see:* per-node progress and a final summary. Files on your Desktop are now organized into subfolders by type.

**Step 4 — Verify:**
```bash
ls ~/Desktop        # you should see typed subfolders, fewer loose files
```

**Step 5 — Make it automatic (every night at 23:00):**
```bash
fusion-cowork schedule add --template desktop_daily_cleanup --cron "0 23 * * *"
```
Now your Desktop tidies itself nightly. List/remove anytime:
```bash
fusion-cowork schedule list
fusion-cowork schedule remove <job_id>
```

**Tweaks:**
- Downloads instead of Desktop → `download_organizer` template (archive + dedupe).
- Free up disk space → `disk_space_cleaner` (caches / temp).
- Find dupes → `duplicate_file_scanner` (reports first, doesn't auto-delete).
- Want it safe? Run risky file ops with `--worktree` (isolated git copy, nothing leaves a recoverable state).

**This scenario used:** templates, `--dry-run`, schedule. No AI. Next try Scenario 2 to see AI build a workflow from a sentence.

### Scenario 2 — One sentence → AI builds the automation

**Situation:** You have a task in your head but no template fits exactly. You'd rather describe it in one sentence and let AI pick the nodes and wire them.

**Prereq:** AI must be reachable. Check + start if needed:
```bash
fusion-cowork ai status
# if unreachable:
~/claude-home/fusion-mlx/start.sh start && ~/claude-home/fusion-mlx/start.sh status
```

**Step 1 — Describe the task, be specific about WHAT and WHERE:**
```bash
fusion-cowork ai generate "Organize all PDFs on my desktop by topic into folders named after the topic"
```
*You'll see:* a generated workflow JSON — the nodes AI chose (scan desktop → AI classify by content → group → move into topic folders) and how they connect. **It does not auto-execute.** You review first.

**Step 2 — Preview the generated workflow:**
```bash
fusion-cowork template run <generated_id> --dry-run
```
*You'll see:* the execution plan with resolved params. Sanity-check the source folder and the classification target.

**Step 3 — Run it:**
```bash
fusion-cowork template run <generated_id>
```

**Step 4 — Verify:**
```bash
ls ~/Desktop        # PDFs now grouped into topic-named folders
```

**Step 5 — Reuse it:** the generated workflow is saved. Schedule it like any template (Scenario 1, Step 5) or re-run on demand.

**Prompt tips (make-or-break):**
- ✅ Specific scope: "Summarize every .md file in ~/notes into one bullet list, save to ~/notes/summary.md"
- ✅ Bilingual: "把下载目录里近 7 天的截图归档到桌面" (node aliases are Chinese-friendly)
- ❌ Vague: "Clean my computer" — AI guesses scope, may miss your intent
- ❌ No node exists: "Send an email to my boss" — it'll fail or hallucinate one

**This scenario used:** `ai generate`, node aliases, the same run/preview/schedule loop. Next: batch a whole folder of documents (Scenario 3).

### Scenario 3 — Batch-process a pile of documents

**Situation:** A folder full of PDFs / Word / Markdown files. You want to summarize them all, classify them by content, and rename them meaningfully — not manually.

**Prereq:** AI reachable (see Scenario 2 prereq).

**Step 1 — Summarize a batch into one report:**
```bash
fusion-cowork template run document_batch_summarizer --dry-run     # preview source folder + output
fusion-cowork template run document_batch_summarizer
```
*You'll see:* each doc summarized, then a combined summary report saved to your chosen output path.

**Step 2 — Classify them by semantic content:**
```bash
fusion-cowork template run ai_smart_file_classification --dry-run
fusion-cowork template run ai_smart_file_classification
```
*You'll see:* files grouped by what they're actually about (not just extension) and moved into content-based folders.

**Step 3 — Rename them from content (no more `untitled-3.pdf`):**
```bash
fusion-cowork template run ai_batch_rename --dry-run
fusion-cowork template run ai_batch_rename
```
*You'll see:* each file renamed from its content — meaningful names you can search.

**Step 4 — Verify:**
```bash
ls <your-docs-folder>     # summarized report present, content-grouped folders, meaningful names
```

**Step 5 — Or let AI build a custom one:** if the three templates don't fit your exact flow, chain them via AI:
```bash
fusion-cowork ai generate "Summarize every PDF in ~/Documents/research, classify by topic, rename each from its content, save the summary to ~/Documents/research/_summary.md"
```

**Tweaks:**
- Run all three nightly → schedule each (Scenario 1, Step 5).
- Image batch instead of docs → `image_batch_rename` (no AI).

**This scenario used:** the three AI templates + `ai generate` as a custom chain. Next: automate a website (Scenario 4).

### Scenario 4 — Automate a website (CDP)

**Situation:** You want to drive a real browser headlessly — open a page, find a button, click it, fill a form, screenshot the result, read the console. Two engines:

- **External Chrome** (default) — drives Chrome via DevTools Protocol on `:9222`.
- **Embedded fusion-browser** (Swift WKWebView, `fusion://` protocol) — build + start it, then set `FUSION_BROWSER_CDP=<port>` to switch targets. See v0.4.2 release notes.

**Step 1 — Open a page and see its structure:**
```bash
fusion-cowork cdp navigate https://example.com
fusion-cowork cdp snapshot
```
*You'll see:* the accessibility tree — a numbered list of elements on the page. Note the number (e.g. `42`) of the element you want to interact with.

**Step 2 — Click and fill:**
```bash
fusion-cowork cdp click 42
fusion-cowork cdp fill --selector "#search" --value "hello world"
```

**Step 3 — Capture the result:**
```bash
fusion-cowork cdp screenshot --save ~/Desktop/shot.png
fusion-cowork cdp evaluate "document.title"
```
*You'll see:* a saved screenshot and the page title printed.

**Step 4 — Wire it into a workflow:** the 10 CDP nodes (Navigate / Snapshot / Click / Fill / FillForm / Screenshot / Evaluate / Emulate / Network / Console) are first-class nodes — compose them into a reusable workflow with `ai generate` or by editing JSON:
```bash
fusion-cowork ai generate "Open github.com, search for 'fusion-cowork', screenshot the results page, save to ~/Desktop"
```

**Step 5 — Verify + reuse:** preview with `--dry-run`, then run; schedule it if it's a recurring check.

**Tweaks:**
- Fill a whole form at once → `cdp fill-form` (multiple fields in one call).
- Watch network/console while automating → `cdp network` / `cdp console` nodes.
- Headless vs headed → depends on your Chrome launch flags.

**This scenario used:** CDP nodes, snapshot-based targeting, AI-composed browser workflow. Next: make any of these run on a schedule (Scenario 5).

### Scenario 5 — Make a task run automatically every day

**Situation:** You've run a workflow once (any of Scenarios 1–4) and liked it. Now you want it to run on its own — nightly, weekly, on a cron. No AI needed.

**Step 1 — Schedule a workflow on a cron:**
```bash
fusion-cowork schedule add --template desktop_daily_cleanup --cron "0 23 * * *"
```
*You'll see:* a job id and confirmation. Your Desktop will tidy itself at 23:00 every night.

**Step 2 — Manage your scheduled jobs:**
```bash
fusion-cowork schedule list            # see all jobs + their next fire time
fusion-cowork schedule remove <job_id> # stop one
```

**Step 3 — Verify it's armed:**
```bash
fusion-cowork schedule list
```
*You'll see:* your job with a `next_run` timestamp. That's your proof it's scheduled.

**Cron cheatsheet** (5 fields: minute hour day-of-month month day-of-week):
| Want | Cron |
|------|------|
| Every night 23:00 | `0 23 * * *` |
| Every Monday 09:00 | `0 9 * * 1` |
| Every 6 hours | `0 */6 * * *` |
| 1st of month, noon | `0 12 1 * *` |

**Tweaks:**
- Schedule a generated AI workflow → use its generated id in `--template`.
- Schedule a browser check → Scenario 4 workflow on a cron (e.g. screenshot a dashboard hourly).
- Enhanced scheduler (calendar view + stats): `fusion-cowork schedule` exposes calendar + dependency features — see README.

**This scenario used:** the scheduler. Next: let Claude Code drive your Mac (Scenario 6).

### Scenario 6 — Let Claude Code drive your Mac (MCP)

**Situation:** You use Claude Code (or any MCP client) and want it to call fusion-cowork as a tool — read/write files, run terminals, take screenshots, classify, summarize, run workflows — all local, no upload.

**Step 1 — Start the MCP server:**
```bash
# stdio mode — for Claude Code's mcp config
fusion-cowork mcp serve

# HTTP mode — for remote clients, streamable HTTP (spec 2025-03-26)
fusion-cowork mcp serve --transport http --port 11438
```
*You'll see:* the server start, exposing 16 tools (read_file, write_file, list_directory, run_terminal, take_screenshot, clipboard_read/write, send_notification, launch_app, web_search, classify_files, summarize_documents, desktop_cleanup, run_workflow, skill_list, skill_run).

**Step 2 — Wire it into Claude Code:** add an `mcpServers` entry in your Claude Code config pointing at the stdio command. Or import an existing Claude Desktop config:
```bash
fusion-cowork mcp import ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

**Step 3 — Verify:** in Claude Code, ask it to use a fusion-cowork tool, e.g. "take a screenshot of my desktop" or "list the files in ~/Downloads". Claude Code calls the exposed tool; you see the local result inline.

**Step 4 — Run a full workflow from Claude Code:** since `run_workflow` is a tool, Claude Code can trigger any template or generated workflow:
> "Run the desktop_daily_cleanup workflow and tell me what it did"

**Tweaks:**
- Remote control (drive your Mac from another machine) → `fusion-cowork remote serve --port 11439 --token <tok>`, then `fusion-cowork remote attach <session> --url ws://host:11439/control --token <tok>`. TLS with `--tls --tls-cert/--tls-key`.
- Deep research (multi-agent) → `fusion-cowork research run "<question>" --depth 3 --max-sources 3`.
- Code review (multi-agent) → `fusion-cowork review run <file> --lens security --lens correctness`.

**This scenario used:** MCP server, tool exposure, Claude Code integration, remote control. Next: team collaboration (Scenario 7).

### Scenario 7 — Team collaboration / multi-agent research

**Situation:** You want a shared workspace where multiple people and AI agents work together — shared chat, artifacts, knowledge base — or you want several agents to split a research/code-review task.

**Prereq:** AI reachable for the agent features (see Scenario 2 prereq).

**Step 1 — Create a collaboration space:**
```bash
fusion-cowork space create --name "Project Alpha" --owner-id user1
fusion-cowork space list
```
*You'll see:* a space id. This is the shared workspace container.

**Step 2 — Invite teammates:**
```bash
fusion-cowork space member invite <space_id> --inviter-id user1 --role member
# teammate joins with the invite code:
fusion-cowork space member join <invite_code> --user-id user2
fusion-cowork space member list <space_id>
```

**Step 3 — Chat + share knowledge:**
```bash
fusion-cowork space chat <space_id> --user user1 --agent agent1
fusion-cowork space knowledge bind <space_id> --operator user1
fusion-cowork space knowledge upload <space_id> doc.pdf --operator user1
fusion-cowork space knowledge search <space_id> "query text" --top-k 5
```
*You'll see:* the space has a knowledge base (via fusion-rag) members can search, plus SSE chat.

**Step 4 — Run multi-agent research or code review:** the orchestrator splits a task across agents with roles (PLANNER / EXECUTOR / etc.) over a message bus:
```bash
fusion-cowork research run "Compare MLX vs llama.cpp inference on Apple Silicon" --depth 3 --max-sources 3
fusion-cowork review run fusion_cowork/engine/workflow.py --lens security --lens correctness
fusion-cowork orchestrator run "Analyze and summarize all repos in ~/code"
```
*You'll see:* agents decompose the task, each contributes, results are synthesized.

**Step 5 — Verify:** `fusion-cowork space get <space_id>` shows members, artifacts, chat history, knowledge status. `fusion-cowork space archive <space_id>` when the project's done.

**Tweaks:**
- Real-time presence + cursors → WebSocket layer: `fusion-cowork collab serve --port 11439`, or `ws://host:8000/spaces/{space_id}/ws`.
- Permissions are 4-tier (view/edit/share/transfer) — manage via `fusion-cowork permission ...`.

**This scenario used:** collaboration space, members, knowledge, orchestrator, research/review. Next: deploy as a cloud service (Scenario 8).

### Scenario 8 — Deploy as multi-tenant cloud SaaS

**Situation:** You're not just running locally — you want to deploy fusion-cowork as a multi-tenant service for many users, with tenant isolation, JWT auth, Postgres, observability, and k8s.

**The four locked architecture decisions (don't re-derive these):**
1. **Persistence** = PostgreSQL (asyncpg pool; SQLite stays as local/test backend).
2. **Isolation** = row-level `tenant_id` column + query guards on every CRUD + Postgres RLS as defense-in-depth. Single schema.
3. **Auth** = JWT (PyJWT HS256/RS256, JWKS fetch + cache) + static-token dev fallback. `FUSION_REQUIRE_JWT=1` forces JWT in prod.
4. **Deploy** = Kubernetes + Helm (prod) or docker-compose (local dev).

**Step 1 — Install cloud extras:**
```bash
pip install -e ".[cloud,web]"      # pyjwt, asyncpg, cryptography, prometheus-client, opentelemetry, structlog
```

**Step 2 — Local cloud dev (docker-compose):**
```bash
docker-compose up -d postgres      # postgres:16 on :5432
export FUSION_PG_DSN="postgresql://fusion:fusion@localhost:5432/fusion"
export FUSION_JWT_SECRET="your-hs256-secret"
fusion-cowork serve --container    # binds 0.0.0.0, SIGTERM graceful drain
```
*You'll see:* the server up on 0.0.0.0, health at `/health` (checks DB / disk / upstream MLX), migrations applied (`schema_migrations` table), RLS policies enabled.

**Step 3 — Verify isolation + health:**
```bash
curl http://localhost:11438/health    # {status: ok|degraded, checks:{db,disk,mlx}}
```
Each tenant sees only its own rows — `tenant_id` query guards + RLS enforce it even if a query forgets the guard.

**Step 4 — Deploy to k8s (Helm):**
```bash
helm template deploy/helm/fusion-cowork > preview.yaml   # dry-render, inspect
helm install fusion-cowork deploy/helm/fusion-cowork      # install
```
*The chart ships:* liveness + readiness probes (on `/health`), HPA (CPU 70%, min 2 max 10), `preStop` + `terminationGracePeriodSeconds: 30` for graceful drain, Secrets for JWT + PG-DSN, ConfigMap for non-secret config.

**Step 5 — Verify in-cluster:**
```bash
kubectl get pods -l app=fusion-cowork
kubectl port-forward svc/fusion-cowork 11438:11438
curl http://localhost:11438/health
```

**Tweaks:**
- Observability: structlog JSON logs + OpenTelemetry traces (OTLP exporter via `OTEL_EXPORTER_OTLP_ENDPOINT`) + Prometheus metrics (env `FUSION_METRICS_PORT` → `/metrics`).
- Security at scale: per-tenant rate limiting, encryption-at-rest (`FUSION_ENCRYPTION_KEY`), tamper-evident audit chain, circuit breaker around MLX/KB clients, per-tenant quotas.
- Plugin hardening: sign manifests with Ed25519 (`FUSION_PLUGIN_SIGNING_KEY`), version-downgrade rejection, persistent registry.
- Backups: `fusion-cowork db backup` (`pg_dump` → `.sql.gz`) / `db restore`.

**This scenario used:** cloud extras, Postgres + RLS, JWT, docker-compose, Helm, observability, security-at-scale. You've now seen the full local-to-cloud range — back to [Part 5](#part-5--going-further) for where to go next.

---

## Part 4 — Troubleshooting decision tree

```
Is it a 401 / auth error calling AI?
├─ YES → You likely put the fusion-mlx key into FUSION_MLX_API_KEY.
│        That variable wants the fusion-gateway CLIENT key
│        (from gateway config.yaml auth.api_keys[].key), NOT the mlx backend key.
│        Fix: export FUSION_MLX_API_KEY="<gateway-client-key>"
│        Two separate auth layers — see README "鉴权说明".

Is fusion-mlx unreachable (ai status = down)?
├─ YES → Start it: ~/claude-home/fusion-mlx/start.sh start
│        Verify:   ~/claude-home/fusion-mlx/start.sh status
│        Port busy: ~/claude-home/fusion-mlx/start.sh stop, then start.

Is "command not found" / plugin ImportError / desk rpc -32603?
├─ YES → Wrong venv. start.sh and the server MUST run on the monorepo root venv:
│        source /Users/dahai/fusion/.venv/bin/activate
│        Then re-run.

Are fewer than 47 nodes visible (e.g. only 33)?
├─ YES → Node modules self-register on import; the server must call import_all_nodes().
│        DeskRPCServer.start() and the desk CLI do this. Custom entry point?
│        Call fusion_cowork.nodes.import_all_nodes() first.

Did a high-risk node get denied / ask for confirmation?
├─ YES → PermissionLevel.CONFIRM is the default (usable + safe). High-risk nodes
│        (~30: shell, file move/delete, disk cleaner, mouse/keyboard, cdp_*, ...)
│        are denied unless you approve. Change mode:
│        fusion-cowork permission level <manual|auto|plan|bypass>
│        Approve permanently: fusion-cowork permission approve <tool> --scope <scope>

Did a file-mutating workflow do the wrong thing?
├─ YES → You skipped --dry-run. Always preview first:
│        fusion-cowork template run <id> --dry-run
│        Risky file ops: add --worktree to run in an isolated git copy.

Still stuck?
└─ Run the doctor: ./start.sh doctor   (venv, CLI, socket, upstream services)
```

---

## Part 5 — Going further

You've finished a scenario end to end. From here:

- **Full command reference** — every CLI command and env var: [README](../README.md) / [README_CN](../README_CN.md). This guide is the on-ramp; the README is the complete catalog.
- **API signatures** (Workflow, WorkflowEngine, Orchestrator, MCP, ReportGenerator): [api.md](api.md).
- **All 47 nodes** with params: README → "Node Reference" tables by category.
- **Write your own node** (Python): subclass `BaseNode`, decorate `@register_node`, drop in `nodes/<category>/`. See existing nodes as templates.
- **3rd-party node packages** (plugins): sign the manifest (Ed25519), `fusion-cowork plugin install /path/to/plugin`. Sandboxed plugins run out-of-process.
- **Multi-tenant cloud** deep dive: README → "☁️ Multi-tenant Cloud SaaS (v0.4.0)".
- **Contributing / tests**: README → "Running Tests", "Contributing". Lint `ruff check .`, tests `pytest tests/ -v`.

**One-line habit to keep:** `--dry-run` before execute, `./start.sh doctor` when stuck.
