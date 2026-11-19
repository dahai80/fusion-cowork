"""Feedback store for the support mailbox agent (issue #546).

P0 observe-only persistence. Primary storage is a local SQLite database
(aiosqlite, same pattern as space/store.py) so the agent works without the
ops PG instance; when FUSION_OPS_DSN is configured the same rows are
best-effort mirrored into the fusion_ops.feedback table (asyncpg, lazy
import — mirror failure logs a warning and never blocks classification).

Idempotency contract: message_id is UNIQUE — a duplicate fetch (same
message-id, poller crash/retry) inserts nothing and returns the existing
row (issue #546 acceptance).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    from_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'unclassified',
    sentiment TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    routed_to TEXT NOT NULL DEFAULT '',
    human_queue INTEGER NOT NULL DEFAULT 0,
    rule_prefilter TEXT NOT NULL DEFAULT '',
    model_raw TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'classified',
    classified_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_message_id ON feedback(message_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class FeedbackStore:
    # P0 note: fusion_ops.feedback (PG) mirror is best-effort when FUSION_OPS_DSN
    # is set; SQLite is the durable primary so observe-only runs never lose rows
    # to ops-DB outages.
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base = os.environ.get("FUSION_COWORK_HOME", "").strip() or str(Path.home() / ".fusion-cowork")
            db_path = str(Path(base) / "mailbox" / "feedback.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"feedback store ready: {self.db_path}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _mirror_pg(self, row: Dict[str, Any]) -> None:
        # Best-effort mirror into fusion_ops.feedback (spec §6) — asyncpg only
        # when FUSION_OPS_DSN present; failure = warning, never raises.
        dsn = os.environ.get("FUSION_OPS_DSN", "").strip()
        if not dsn:
            return
        try:
            import asyncpg

            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(
                    """
                    INSERT INTO feedback (message_id, from_addr, subject, received_at, category,
                                          sentiment, urgency, confidence, routed_to, human_queue, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    row["message_id"],
                    row.get("from_addr", ""),
                    row.get("subject", ""),
                    row.get("received_at", ""),
                    row.get("category", ""),
                    row.get("sentiment", ""),
                    row.get("urgency", ""),
                    row.get("confidence", 0.0),
                    row.get("routed_to", ""),
                    bool(row.get("human_queue", False)),
                    row.get("status", "classified"),
                )
            finally:
                await conn.close()
        except Exception as e:
            logger.warning(f"feedback PG mirror skipped (fusion_ops.feedback unavailable): {e}")

    async def record_message(self, msg: Dict[str, Any]) -> Optional[int]:
        # Insert one fetched message; duplicate message-id returns None (no row).
        await self.connect()
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        cur = await self._db.execute(
            """
            INSERT OR IGNORE INTO feedback
                (message_id, from_addr, subject, received_at, category, created_at)
            VALUES (?, ?, ?, ?, 'unclassified', ?)
            """,
            (
                msg.get("message_id", ""),
                msg.get("from_addr", ""),
                msg.get("subject", ""),
                msg.get("received_at", ""),
                now,
            ),
        )
        await self._db.commit()
        if cur.rowcount == 0:
            logger.debug(f"duplicate message-id skipped: {msg.get('message_id', '')}")
            return None
        row_id = cur.lastrowid
        await self._mirror_pg({**msg, "message_id": msg.get("message_id", "")})
        return row_id

    async def record_classification(self, message_id: str, classification: Dict[str, Any]) -> bool:
        await self.connect()
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        cur = await self._db.execute(
            """
            UPDATE feedback SET category=?, sentiment=?, urgency=?, confidence=?,
                                routed_to=?, human_queue=?, rule_prefilter=?, model_raw=?,
                                status='classified', classified_at=?
            WHERE message_id=?
            """,
            (
                classification.get("category", "unclassified"),
                classification.get("sentiment", ""),
                classification.get("urgency", ""),
                float(classification.get("confidence", 0.0)),
                classification.get("routed_to", ""),
                1 if classification.get("human_queue") else 0,
                classification.get("rule_prefilter", ""),
                classification.get("model_raw", "")[:4000],
                now,
                message_id,
            ),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def get_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        await self.connect()
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM feedback WHERE message_id=?", (message_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_since(self, since_iso: str, limit: int = 500) -> List[Dict[str, Any]]:
        await self.connect()
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM feedback WHERE created_at >= ? ORDER BY created_at LIMIT ?",
            (since_iso, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def count_category_since(self, since_iso: str) -> Dict[str, int]:
        await self.connect()
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT category, COUNT(*) AS n FROM feedback WHERE created_at >= ? GROUP BY category",
            (since_iso,),
        )
        rows = await cur.fetchall()
        return {r["category"]: r["n"] for r in rows}

    async def get_state(self, key: str) -> str:
        await self.connect()
        assert self._db is not None
        cur = await self._db.execute("SELECT value FROM poll_state WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else ""

    async def set_state(self, key: str, value: str) -> None:
        await self.connect()
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO poll_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._db.commit()
