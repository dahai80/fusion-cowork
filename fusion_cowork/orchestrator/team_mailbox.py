"""Durable team-message inbox (issue #190) — isomorphic to agent-studio durable_roster.

The mailbox table is fully isomorphic to agent-studio's: both daemons point
at the same FUSION_TEAM_DB file for cross-repo interoperability
(cowork agent ↔ agent-studio agent).
ack semantics are at-least-once: a message is acked only after dispatch
succeeds, so crash replay loses no message.

cowork keeps its own roster in the cowork_roster table — the agent-studio
roster table semantics are untouched (its inbox consumer grabs messages via
autonomous_inbox); the two daemons each manage their own roster and share
one mailbox.

issue #494 (K4) kernel convergence note: fusion_harness.team_runtime
TeamRuntime.send is also disk-write-confirmed (persist failure rolls back
+ delivered=false), but it CANNOT replace this mailbox here:
- the mailbox table IS the cross-repo wire protocol — agent-studio's
  daemon consumes this exact SQLite schema via shared FUSION_TEAM_DB;
- kernel MSG_TYPES is a closed 4-value set (text/plan_approval/
  shutdown_request/shutdown_response) — cowork kinds ("message"/"reply")
  don't map;
- kernel send() requires sender+recipient to be declared team members —
  cross-repo senders are not. TeamMailbox stays the protocol owner;
  row_to_team_message() exports rows as kernel TeamMessage for kernel-typed
  consumers (kind unmappable to MSG_TYPES falls back to "text").
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB = str(Path.home() / ".fusion-cowork" / "team.db")
_MAX_BODY = 20000

# mailbox table isomorphic to agent-studio agent_runtime/durable_roster.py (cross-repo protocol)
_MAILBOX_SQL = """
    CREATE TABLE IF NOT EXISTS mailbox (
        message_id TEXT PRIMARY KEY,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        team TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'message',
        body TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        acked_at REAL NOT NULL DEFAULT 0
    )
"""
_MAILBOX_INDEX_SQL = "CREATE INDEX IF NOT EXISTS mailbox_recipient ON mailbox(recipient, acked_at)"

_COWORK_ROSTER_SQL = """
    CREATE TABLE IF NOT EXISTS cowork_roster (
        agent_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        team TEXT NOT NULL DEFAULT '',
        workflow_id TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )
"""


def resolve_db_path(path: Optional[str] = None) -> str:
    # FUSION_COWORK_TEAM_DB first, then reuse agent-studio's FUSION_TEAM_DB (shared mailbox)
    return str(path or os.environ.get("FUSION_COWORK_TEAM_DB") or os.environ.get("FUSION_TEAM_DB") or DEFAULT_DB)


def row_to_team_message(row: Dict[str, Any]) -> Any:
    # issue #494: export a mailbox row as a kernel TeamMessage for kernel-typed
    # consumers. Lossy by design: kind → kernel MSG_TYPES ("message"/"reply" and
    # other cowork kinds fall back to "text"); team/acked_at/created_at have no
    # kernel counterpart (caller keeps the row for those).
    from fusion_harness.team_runtime import MSG_TYPES, TeamMessage

    kind = str(row.get("kind", "message"))
    msg_type = kind if kind in MSG_TYPES else "text"
    return TeamMessage(
        type=msg_type,
        sender=str(row.get("sender", "")),
        recipient=str(row.get("recipient", "")),
        body=str(row.get("body", "")),
        msg_id=str(row.get("message_id", "")),
    )


@contextmanager
def _connect(path: str):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


class TeamMailbox:
    """Durable point-to-point mailbox — send/inbox/ack; messages are never lost before ack."""

    def __init__(self, path: Optional[str] = None):
        self.path = resolve_db_path(path)
        self._init()
        # In-process push hook: once the runner pool registers wake(), a local send wakes consumption immediately
        self.on_send = None
        logger.info(f"team mailbox ready: {self.path}")

    def _init(self) -> None:
        with _connect(self.path) as conn:
            conn.execute(_MAILBOX_SQL)
            conn.execute(_MAILBOX_INDEX_SQL)

    def send(
        self,
        sender: str,
        recipient: str,
        body: str = "",
        kind: str = "message",
        team: str = "",
    ) -> Dict[str, Any]:
        if not sender or not recipient:
            raise ValueError("sender and recipient are required")
        message_id = uuid.uuid4().hex[:16]
        now = time.time()
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO mailbox
                    (message_id, sender, recipient, team, kind, body, created_at, acked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (message_id, sender, recipient, team, kind, str(body)[:_MAX_BODY], now),
            )
        logger.info(f"mailbox delivery: {message_id} {sender} → {recipient} kind={kind}")
        hook = self.on_send
        if hook is not None:
            try:
                hook(recipient)
            except Exception as e:
                logger.warning(f"mailbox push hook failed (ignored) {recipient}: {e}")
        return {
            "message_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "team": team,
            "kind": kind,
            "body": body,
            "created_at": now,
            "acked_at": 0,
        }

    def inbox(self, recipient: str, unread_only: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT * FROM mailbox WHERE recipient = ?"
        if unread_only:
            query += " AND acked_at = 0"
        query += " ORDER BY created_at ASC LIMIT ?"
        with _connect(self.path) as conn:
            rows = conn.execute(query, (recipient, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def ack(self, recipient: str, message_id: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute(
                "UPDATE mailbox SET acked_at = ? WHERE message_id = ? AND recipient = ?",
                (time.time(), message_id, recipient),
            )
        ok = cur.rowcount > 0
        logger.info(f"mailbox ack: {message_id} recipient={recipient} ok={ok}")
        return ok

    def depth(self, recipient: str = "") -> int:
        with _connect(self.path) as conn:
            if recipient:
                row = conn.execute(
                    "SELECT COUNT(*) FROM mailbox WHERE recipient = ? AND acked_at = 0", (recipient,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM mailbox WHERE acked_at = 0").fetchone()
        return int(row[0]) if row else 0

    def oldest_unacked_age(self, recipient: str = "") -> float:
        with _connect(self.path) as conn:
            if recipient:
                row = conn.execute(
                    "SELECT MIN(created_at) FROM mailbox WHERE recipient = ? AND acked_at = 0", (recipient,)
                ).fetchone()
            else:
                row = conn.execute("SELECT MIN(created_at) FROM mailbox WHERE acked_at = 0").fetchone()
        oldest = row[0] if row else None
        if oldest is None:
            return 0.0
        return max(0.0, time.time() - float(oldest))

    def stats(self) -> Dict[str, Any]:
        with _connect(self.path) as conn:
            pending = conn.execute("SELECT COUNT(*) FROM mailbox WHERE acked_at = 0").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
        return {"total": int(total), "pending": int(pending), "path": self.path}


class CoworkRoster:
    """cowork-side durable roster — only agents registered here are consumed by this repo's consumer."""

    def __init__(self, path: Optional[str] = None):
        self.path = resolve_db_path(path)
        self._init()
        logger.info(f"cowork roster ready: {self.path}")

    def _init(self) -> None:
        with _connect(self.path) as conn:
            conn.execute(_MAILBOX_SQL)
            conn.execute(_MAILBOX_INDEX_SQL)
            conn.execute(_COWORK_ROSTER_SQL)

    def register(
        self,
        agent_id: str,
        workflow_id: str,
        name: str = "",
        team: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        agent_id = (agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id is required")
        if not workflow_id:
            raise ValueError(f"agent {agent_id} missing workflow_id binding (which workflow to run on receipt)")
        now = time.time()
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO cowork_roster
                    (agent_id, name, team, workflow_id, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name, team=excluded.team,
                    workflow_id=excluded.workflow_id,
                    enabled=excluded.enabled, updated_at=excluded.updated_at
                """,
                (agent_id, name or agent_id, team, workflow_id, int(bool(enabled)), now),
            )
        logger.info(f"roster registered: agent={agent_id} workflow={workflow_id} team={team} enabled={enabled}")
        return self.get(agent_id) or {}

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with _connect(self.path) as conn:
            row = conn.execute("SELECT * FROM cowork_roster WHERE agent_id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def list(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM cowork_roster"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY agent_id"
        with _connect(self.path) as conn:
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def remove(self, agent_id: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute("DELETE FROM cowork_roster WHERE agent_id = ?", (agent_id,))
        ok = cur.rowcount > 0
        logger.info(f"roster removed: agent={agent_id} ok={ok}")
        return ok
