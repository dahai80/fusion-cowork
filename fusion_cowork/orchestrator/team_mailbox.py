"""团队消息 durable 收件箱 (issue #190) — 与 agent-studio durable_roster 同构。

mailbox 表与 agent-studio 完全同构: 双方 daemon 指向同一个
FUSION_TEAM_DB 文件即可跨仓互通 (cowork agent ↔ agent-studio agent)。
ack 语义 at-least-once: 消息派发成功之后才 ack, 崩溃重放不丢消息。

cowork 自己的 roster 落在 cowork_roster 表 — 不改 agent-studio 的
roster 表语义 (其 inbox consumer 按 autonomous_inbox 抢消息), 两个
daemon 各管各的花名册, 共用一个信箱。
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

# mailbox 表与 agent-studio agent_runtime/durable_roster.py 同构 (跨仓协议)
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
    # FUSION_COWORK_TEAM_DB 优先, 其次沿用 agent-studio 的 FUSION_TEAM_DB (共享信箱)
    return str(path or os.environ.get("FUSION_COWORK_TEAM_DB") or os.environ.get("FUSION_TEAM_DB") or DEFAULT_DB)


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
    """durable 点对点信箱 — send/inbox/ack, ack 前消息永不丢。"""

    def __init__(self, path: Optional[str] = None):
        self.path = resolve_db_path(path)
        self._init()
        # 进程内推送钩子: runner pool 注册 wake() 后, 本地发送即刻唤醒消费
        self.on_send = None
        logger.info(f"团队信箱就绪: {self.path}")

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
            raise ValueError("sender 和 recipient 必填")
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
        logger.info(f"信箱投递: {message_id} {sender} → {recipient} kind={kind}")
        hook = self.on_send
        if hook is not None:
            try:
                hook(recipient)
            except Exception as e:
                logger.warning(f"信箱推送钩子失败 (忽略) {recipient}: {e}")
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
        logger.info(f"信箱 ack: {message_id} recipient={recipient} ok={ok}")
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
    """cowork 侧 durable 花名册 — 只有这里登记的 agent 才被本仓 consumer 消费。"""

    def __init__(self, path: Optional[str] = None):
        self.path = resolve_db_path(path)
        self._init()
        logger.info(f"cowork 花名册就绪: {self.path}")

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
            raise ValueError("agent_id 必填")
        if not workflow_id:
            raise ValueError(f"agent {agent_id} 缺 workflow_id 绑定 (收件后跑哪个工作流)")
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
        logger.info(f"花名册登记: agent={agent_id} workflow={workflow_id} team={team} enabled={enabled}")
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
        logger.info(f"花名册移除: agent={agent_id} ok={ok}")
        return ok
