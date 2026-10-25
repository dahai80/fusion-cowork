# Node Executors and Parallel Scheduling (issue #189)

## Overview

A workflow node is no longer required to run its own `execute()`. The engine looks up
a **node executor** for every node through `fusion_cowork/engine/executors.py` and calls
`executor.execute(node, inputs, ctx)`. Two executors ship today:

| executor | behaviour |
|---|---|
| `simple` (default) | calls `node.execute(inputs)` — the historical path, zero behaviour change |
| `agent` | hands the task text to a fusion-agent-studio agent, which runs a real multi-turn tool loop |

The executor is chosen per node (`config.executor`), so a single DAG can mix deterministic
nodes and agentic nodes. Unknown executor names log a WARNING and fall back to `simple`
rather than failing the run.

Execution also has a **ready-set wave scheduler**: nodes whose predecessors are all done
run in the same wave, up to the configured concurrency. With concurrency `1` the engine
behaves exactly like the historical serial topological loop.

## Executor Types

### simple

```json
{
  "id": "n1",
  "name": "shell_exec",
  "config": {
    "executor": "simple",
    "params": {"command": "ls"}
  }
}
```

`SimpleExecutor` only forwards the call. Everything else (timeout, retries, hooks,
permission gate, schema validation) is handled by the engine around the executor, so
switching executor never bypasses those guards.

### agent

```json
{
  "id": "n2",
  "name": "agent_probe",
  "config": {
    "executor": "agent",
    "agent": "review-agent",
    "tools": ["read_file", "web_search"],
    "model": "claude-sonnet-5-5",
    "max_turns": 8,
    "budget_tokens": 50000,
    "params": {"task": "Summarize CHANGELOG.md and list risky changes"}
  }
}
```

`AgentExecutor` derives the task text from the node input first (`input`, `task`,
`prompt`, `goal`, `text`), then from `config.params` (`task`, `prompt`, `input`), and
lastly serialises the whole input dict as JSON. With no task text at all the node fails
with `agent 执行器缺少任务文本 (input/task/prompt)`.

Executor-specific config fields (all optional, serialised by `BaseNode.to_dict()`):

| field | type | meaning |
|---|---|---|
| `executor` | str | `simple` (default) or `agent` |
| `agent` | str | agent-studio agent name/id; defaults to `cowork-<node id prefix>` |
| `tools` | list[str] | tool allowlist forwarded to the agent |
| `model` | str | model override for this node |
| `max_turns` | int | turn cap (default 8) |
| `budget_tokens` | int | token budget, `0` = unlimited |

## Concurrency

Resolution order for the per-run concurrency (`_resolve_concurrency`):

1. `WorkflowEngine.execute(..., concurrency=N)` explicit argument
2. `FUSION_COWORK_MAX_CONCURRENCY` environment variable
3. default `1`

`start.sh` exports `FUSION_COWORK_MAX_CONCURRENCY=4` for the service process, so a
deployed `fusion-cowork desk rpc` / `serve` runs four-way parallel by default while the
library default stays conservatively serial. Set it to `1` to restore exact legacy
scheduling, or lower it if your nodes compete for a single resource.

Scheduling loop, per iteration:

1. honour cancel flag and the W8 pause checkpoint
2. apply queued run directives (`set_params` / `insert_node` / `skip_remaining`)
3. check the budget
4. compute the ready set — `ready = [nid for nid in unscheduled if preds(nid) ⊆ done]`
5. take `ready[:1]` when concurrency is `1`, otherwise `ready[:concurrency]`
6. `await` sequentially for one node, `asyncio.gather` for a real wave
7. append the wave's steps **in topological order** (not completion order) and notify
   progress, so execution records and SSE streams stay deterministic

Failure semantics: a wave's control results merge with `cancel` beating `fail`. A node
that fails with `continue_on_error=False` fails the run, but sibling nodes already in
the same wave finish and their steps are still recorded. Nodes with no incoming edges
and no outgoing edges are legal in a multi-node workflow — validation only logs
`工作流含无连线节点, 将作为独立分支执行`, because parallel independent branches are a
supported shape.

Human-in-the-loop approval is still serialised per run: `_RunState.hitl_lock` wraps the
permission check and `_hitl_await`, so two concurrent nodes cannot interleave their
pending approvals for the same execution id.

## Agent Executor

Transport resolution (`_as_transport()`):

1. `FUSION_COWORK_AGENT_TRANSPORT=uds|http` forces a channel
2. otherwise `uds` when the agent-studio daemon socket exists, else `http`

| variable | default | meaning |
|---|---|---|
| `FUSION_AGENT_STUDIO_SOCKET` | `/tmp/fusion-studio.sock` | agent-studio UDS daemon |
| `FUSION_AGENT_STUDIO_URL` | `http://127.0.0.1:11442` | agent-studio HTTP API |
| `FUSION_AGENT_STUDIO_API_KEY` | — | sent as `x-api-key` on HTTP |
| `FUSION_COWORK_AGENT_TRANSPORT` | auto | force `uds` or `http` |

### UDS channel (full capability)

`subagent.run` with `{spec: {name, task, model, tools, max_turns, budget_tokens}}`
returns `{report: {ok, report, turns, tokens_used, agent_id, partial}}`. The executor
then calls `subagent.transcript` and counts `tool_use` / `tool_call` content blocks, so
the node summary shows the real multi-turn loop:

```
agent 完成 turns=3 tool_calls=2 tokens=120
```

`ok=false` turns the node into a FAILED step with the agent's error; `partial=true`
still succeeds but the summary is annotated `(未达 max_turns 上限, 标记 partial)`.
A missing daemon socket yields `agent-studio daemon socket 不可用 (path): ...` — a
visible failure, never a silent fallback to a plain LLM call.

### HTTP channel (degraded, documented)

HTTP submission is `POST /v1/runs` + poll `GET /v1/runs/{id}` until
`completed`/`failed`/`canceled`, then read `GET /v1/runs/{id}/result`. Because the HTTP
API takes a whole agent definition rather than per-run overrides, `tools`, `model`,
`max_turns` and `budget_tokens` cannot be honoured — the executor logs

```
WARNING AgentExecutor http 通道不支持逐节点覆盖 ['tools', 'model'], 本次运行按 agent-studio 中该 agent 的定义执行
```

and the step summary reports `http 通道不提供 turns`. Use the UDS channel when you need
per-node overrides. A timeout cancels the run best-effort and fails the node with the
`run_id` in the error, so the operator can inspect it in agent-studio.

## Observability

Every `WorkflowStep` carries `executor` (which executor ran the node) and the execution
record carries `concurrency` plus `executors` (a `{name: count}` mix). The run-scoped
log lines (issue #120) pick this up:

```
[run-42] run start: workflow=demo nodes=3 hitl_mode=headless hitl_timeout=1800s concurrency=4 executors={'simple': 2, 'agent': 1}
[run-42] step start: node=collect node_id=n1 executor=simple
[run-42] step end:   node=review node_id=n3 verdict=success duration=12.401s executor=agent params={'task': '...'} error=
[run-42] run end:    outcome=success total=13.902s steps=3 concurrency=4 executors={'simple': 2, 'agent': 1}
```

The final summary string also reports the mix:
`工作流 'demo' 执行完成: 3/3 节点成功, 耗时 13.90s, 并发=4 执行器=simple:2, agent:1`.

Because steps are appended in topological order regardless of completion order,
`grep "[run-42]"` and the REST/SSE views remain stable and diffable between runs.

## Example Workflow

Three independent nodes (no edges) plus a join, running four-way parallel:

```json
{
  "id": "wf-parallel",
  "name": "parallel-demo",
  "nodes": [
    {"id": "a", "name": "shell_exec", "config": {"params": {"command": "sleep 1"}}},
    {"id": "b", "name": "shell_exec", "config": {"params": {"command": "sleep 1"}}},
    {"id": "c", "name": "shell_exec", "config": {"params": {"command": "sleep 1"}}},
    {
      "id": "d",
      "name": "agent_probe",
      "config": {
        "executor": "agent",
        "agent": "review-agent",
        "tools": ["read_file"],
        "params": {"task": "Summarize the outputs of a, b and c"}
      }
    }
  ],
  "edges": [
    {"source_id": "a", "target_id": "d"},
    {"source_id": "b", "target_id": "d"},
    {"source_id": "c", "target_id": "d"}
  ]
}
```

`a`, `b`, `c` form the first wave (wall time ≈ 1s, not 3s with concurrency ≥ 3), then
`d` runs as an agent after all three complete. With concurrency `1` the same definition
runs strictly in topological order — same result, four times the wall time.

The behaviour above is covered by `tests/test_issue_189_node_executor.py`
(`CLAIM: verified by tests/test_issue_189_node_executor.py`).
