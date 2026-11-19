---
doc_id: fusion-cowork/guide/mailbox-agent
product: fusion-cowork
class: platform
entry: service
doc_type: guide
title: {zh: "支持邮箱代理", en: "Support mailbox agent"}
audience: engineer
status: draft
version: 0.5.133
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - docs/mailbox-agent.md
---

# Support Mailbox Agent (issue #546, P0 observe-only)

Support mailbox automation for `support@fusion-mlx.cn` per
`architecture/fusion-flywheel-1119.md` §6 (flywheel Loop C signal source).
Local-first: IMAP stdlib poller + local mlx LLM classification, phased
autonomy.

## Pipeline

```
IMAP poll (5 min, message-id idempotent)
  -> deterministic rule pre-filter (bounces / no-reply / marketing = spam, no LLM)
  -> LLM classify (local mlx) — category + sentiment + urgency + confidence
  -> route record (P0: record only) + feedback row
  -> daily digest mail to owner (the ONLY outbound mail in P0)
```

Categories: `bug` / `feature` / `business` / `data_rights` / `account` /
`consult` / `spam`.

Routing contract (P1 activates sends; P0 records `routed_to` only):

| category | route |
|---|---|
| bug | mined_issues + template auto-ack |
| feature | requirement pool digest |
| business / data_rights | owner notify + SLA ticket |
| consult | FAQ draft for review |
| spam | drop |

## Red lines (spec §6)

- Substantive replies ALWAYS human-approved (P1+).
- Low confidence (< 0.6), `business`/`data_rights` classes, or
  unparseable LLM output = human queue — fail-open to human, never to
  automation.
- Every message becomes a structured feedback row (message-id UNIQUE —
  duplicates are dropped, poller crash-recovery advances a persisted UID
  checkpoint only after a successful fetch).

## Configuration

| env | meaning |
|---|---|
| `FUSION_MAILBOX_ENABLED` | `1` enables the agent (web serve lifecycle or `fusion-cowork mailbox serve`) |
| `FUSION_MAILBOX_IMAP_HOST` / `_PORT` / `_USER` / `_PASS` / `_FOLDER` | IMAP credentials — fusion-sv env injection ONLY, never in the repo |
| `FUSION_MAILBOX_MODEL` | local mlx classify model (default `qwen3.5-9b`) |
| `FUSION_OPS_DSN` | optional — mirrors classified rows best-effort into `fusion_ops.feedback` (PG); primary storage is always local SQLite |

Storage: `~/.fusion-cowork/mailbox/feedback.db` (SQLite; tables `feedback`
+ `poll_state`).

## CLI

```bash
fusion-cowork mailbox serve             # run the agent loop in foreground
fusion-cowork mailbox serve -i 120     # custom poll interval (seconds)
```

The agent also starts/stops with `fusion-cowork web serve` when enabled.

## Phases

- **P0 (this release)**: observe-only — classify + daily digest, zero
  outbound actions.
- **P1** (follow-up issue, gated on >= 90% classification accuracy over
  the P0 window): template auto-ack for bugs, mined_issues integration.
- **P2**: FAQ whitelist auto-reply.
