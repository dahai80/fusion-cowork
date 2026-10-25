# Team Messaging — Durable Mailbox + Resident Consumers

Cross-process agent messaging for fusion-cowork (issue #190). Messages are durable rows in SQLite; every agent listed in the roster consumes its own inbox from a daemon-resident pool, runs a workflow, and replies to the sender. No external RPC pull, no in-memory queue.

## Pieces

| Piece | Module | Role |
|---|---|---|
| `TeamMailbox` | `fusion_cowork/orchestrator/team_mailbox.py` | durable point-to-point inbox: `send` / `inbox` / `ack` / `depth` / `oldest_unacked_age` |
| `CoworkRoster` | same | which cowork agents consume, and which workflow each one runs |
| `RunnerPool` | `fusion_cowork/orchestrator/runner_pool.py` | daemon-resident consumer loop: dispatch, ack, reply, metrics |
| Web API | `fusion_cowork/server/web_api.py` (`/api/team/*`) | roster CRUD, send/inbox, pool lifecycle, notify |
| CLI | `fusion_cowork/cli.py` (`fusion-cowork team ...`) | operator surface |
| Worker | `RunRegistry.start(..., message_input=...)` | runs the bound workflow with the message as first-node input |

## Storage

Default DB: `~/.fusion-cowork/team.db`, overridable by `FUSION_COWORK_TEAM_DB`, then `FUSION_TEAM_DB` (agent-studio's variable) — so pointing both daemons at one file is a single export.

```sql
CREATE TABLE mailbox (
    message_id TEXT PRIMARY KEY,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    team       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'message',
    body       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    acked_at   REAL NOT NULL DEFAULT 0
);
CREATE INDEX mailbox_recipient ON mailbox(recipient, acked_at);

CREATE TABLE cowork_roster (
    agent_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    team        TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL
);
```

`mailbox` is byte-compatible with `fusion-agent-studio/agent_runtime/durable_roster.py`, which is the cross-repo wire contract. Cowork keeps its roster in `cowork_roster` rather than the shared `roster` table: agent-studio's `InboxConsumer` claims rows by `autonomous_inbox` + `graph_id`, so writing cowork agents there would make the two daemons steal each other's messages. Both daemons read/write the same `mailbox`; each owns its roster.

## Delivery semantics

1. `tick()` reads every enabled roster agent and its unread messages (oldest first, limit 100).
2. For each message: skip if already in flight for this message id; call `dispatch(agent, message)`.
3. `dispatch` returns a run id **only after** `RunRegistry.start` accepted the run. On exception or empty run id: no ack, `dispatch_errors += 1`, the message stays unread and is retried on the next tick.
4. After a successful dispatch: `ack(recipient, message_id)`, `consumed += 1`, and the run id is registered for reply collection.
5. When the run reaches a terminal status, a `reply` message is written to the original sender (`kind="reply"`, body `[run <message_id>] <result summary>`). Failed/cancelled runs are not replied to; the in-flight entry is still released.

Consequences:

- **At-least-once**: a crash between dispatch and ack redelivers. Consumers must be idempotent or the workflow must tolerate reruns.
- **No double dispatch inside one process**: `_in_flight[message_id] = run_id` shields the message until the run is terminal.
- **Body cap**: 20 000 chars at send; replies capped at 4 000.

## Delivery triggers

- **Push (local)** — `TeamMailbox.on_send` is wired to `RunnerPool.wake()`; a send from the same process consumes almost immediately.
- **Push (remote)** — peer daemons call `POST /api/team/notify` after writing to the mailbox.
- **Poll (fallback)** — `FUSION_INBOX_POLL_INTERVAL` (seconds, default 5). Covers peers with no notify hook; also the safety net if a wake is missed.

## Message payload → workflow input

`dispatch` loads the workflow definition from the workflow store and builds the first-node input:

```json
{
    "input": "make report",
    "sender": "alice",
    "message_id": "1f0c...",
    "team": "qa"
}
```

- Body that parses as a JSON object becomes the payload; every key is passed through.
- Non-JSON body → `{"input": "<raw body>"}`.
- `sender` / `message_id` / `team` are always set (overriding same-named keys from the body), so a workflow can address the reply path.
- The payload is passed as `message_input` to `RunRegistry.start` and merged with (not replaced by) memory recall context.

## Lifecycle

- `web serve` startup: if the roster has at least one enabled agent, the pool starts; otherwise it logs that it stayed down. `FUSION_COWORK_RUNNER_POOL=1` forces it up regardless.
- Shutdown stops the pool (`asyncio` task cancelled; unacked messages stay in SQLite).
- Manual: `POST /api/team/pool/start` / `stop`, or `fusion-cowork team start|stop`.

## Metrics

`GET /api/team/status`:

```json
{
    "running": true,
    "poll_interval": 5.0,
    "consumers": 1,
    "agents": [
        {"agent_id": "reviewer", "workflow_id": "wf-review", "team": "qa", "enabled": true,
         "inbox_depth": 0, "oldest_unacked_age_s": 0.0, "consumed": 3, "replies_sent": 3}
    ],
    "metrics": {"consumed": 3, "replies_sent": 3, "dispatch_errors": 0,
                "mailbox_depth": 0, "oldest_unacked_age_s": 0.0, "per_agent": {...}}
}
```

Export to OTLP (via `fusion_cowork.observability.metrics.get_meter`):

- histogram `fusion_team_mailbox_depth`
- histogram `fusion_team_oldest_unacked_age_seconds`
- counter `fusion_team_mailbox_consumed_total` (delta-only adds, never replays the cumulative value)
- counter `fusion_team_mailbox_replies_total` (delta-only)

Alert on a rising `oldest_unacked_age_seconds`: it means the pool is stopped or every dispatch is failing.

## Cross-project with fusion-agent-studio

Both sides share the mailbox file:

```bash
export FUSION_TEAM_DB=$HOME/.fusion-team/team.db
fusion-cowork web serve
# agent-studio daemon started with the same FUSION_TEAM_DB
```

- cowork → agent-studio: cowork writes `recipient=<studio agent id>`; agent-studio's `InboxConsumer` picks it up if that agent has `autonomous_inbox=1` and a `graph_id` in its own roster.
- agent-studio → cowork: agent-studio writes into the shared mailbox; cowork picks it up on the next tick. For sub-second latency, have the sender call cowork's `/api/team/notify` afterwards — agent-studio has no notify hook yet, so the poll interval is the default path.

## Worked example

```bash
# 1. workflow that consumes the message input
fusion-cowork web serve &
curl -X POST localhost:11461/api/workflows -H 'Authorization: Bearer <key>' \
  -d '{"id": "wf-review", "name": "PR review", "workflow": {"nodes": [], "edges": []}}'

# 2. register the resident consumer
fusion-cowork team register reviewer --workflow wf-review --team qa

# 3. send a request (wakes the pool immediately)
fusion-cowork team send --from alice --to reviewer '{"input": "review PR 42"}'

# 4. watch it happen
fusion-cowork team status
fusion-cowork team inbox alice      # reply appears after the run reaches success
```

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| `dispatch_errors` climbing, message stays unread | bound workflow id missing from the store, or `RunRegistry.start` raised | `fusion-cowork workflow list`; check daemon log for `消费池派发失败` |
| Pool `running: false` with agents registered | started without `web serve` lifecycle, or manually stopped | `fusion-cowork team start` |
| Reply never arrives but consumed grows | run failed/cancelled (no reply by design) | inspect `GET /api/runs`; failures are logged as `消费池 run 未成功` |
| Same message processed twice after a crash | at-least-once redelivery | make the workflow idempotent |
