# Run State Recovery (issue #191)

Durable run control state for the web engine: paused/resumed status, queued
directives, pending-approval boundaries, and completed step outputs survive a
daemon restart — including `kill -9` — and are restored through a single
recovery path.

## Storage

- SQLite at `<store_root>/run_states.db` (default
  `~/.fusion-cowork/web/run_states.db`), managed by
  `fusion_cowork.engine.run_persistence.RunStateStore`.
- One row per **active** run (`status` in `running`/`paused`). Terminal rows
  are deleted by `RunRegistry` when the run ends; stale terminal rows found at
  load are cleaned with a WARNING.
- Schema version is tracked via `PRAGMA user_version`. Opening a database
  written by a **newer** fusion-cowork raises `RuntimeError` at startup —
  never a silent downgrade read.
- Every engine event is a checkpoint: `RunRegistry._on_event` → `_persist`
  writes the full record (workflow def, step snapshots, directives, status,
  `paused_reason`). Persist failures only WARN — persistence is a side path
  and never blocks execution.

## Recovery

`RunRegistry.recover_persisted()` runs on FastAPI startup (`create_web_app`):

1. Loads all active rows. Rows whose `run_id` is already live in memory are
   skipped; rows that cannot be parsed (bad JSON, node-less workflow def) are
   logged at ERROR and **deleted** — never silently retried.
2. Each row is rebuilt as a `paused` record and the engine task is restarted
   with `start_paused=True` + `resume_steps=<step snapshots>` — the same
   resume path used by in-process pause/resume, so step-level progress and
   control state recover through one code path.
3. Persisted directives are **replayed** into the new engine's directive
   queue (full payload, logged per directive). They take effect at the first
   node boundary after `resume`.
4. A row persisted as `running` means the previous session died without
   cleanup (`kill -9`, crash). Recovery logs a loud WARNING and restores the
   run as `paused` with `paused_reason="crash"`. A row persisted as `paused`
   keeps its original reason (`manual`/`hitl`).

Recovered runs never auto-resume. They wait for a human `POST
/api/runs/{id}/resume` or, for runs that were waiting on an approval, the
re-executed node re-enters the HITL boundary and a fresh approval completes it.

## What survives / what does not

**Survives**: workflow definition, completed step outputs (snapshot-skip on
resume), queued directives, paused status and reason, run identity/creation
time.

**Does not survive** (documented, loud):
- In-flight node partial execution — the interrupted node restarts from
  scratch on resume.
- Unconfirmed approval decisions — HITL boundaries re-enter and wait again.
- SSE subscribers — clients must re-subscribe (`GET /api/runs/{id}/events`).

## Memory-bridge note

Recovered runs skip the memory `retrieve_context` injection: the first node's
snapshot already contains the injected context from the original start, and
re-injecting would overwrite completed step outputs.

## Back-compat

- `RunRegistry` without an injected `run_store` behaves exactly as before
  (pure in-memory, zero persistence).
- `engine.execute()` receives `resume_steps`/`start_paused` only when set, so
  injected legacy engines without the new keyword arguments keep working.
