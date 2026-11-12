"""Durable HITL approval journal (issue #463).

审批决定日志 — SQLite 落盘, 服务器崩溃后审批不丢: 待审批在闸门入口记录,
决定 (approve/deny/timeout) 落库; 重启恢复时 pending 行可重建进审批队列,
已有决定在闸门重入时直接重放 (durable resumable wait)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_journal (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    node_name TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    workflow TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    ts REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    approved INTEGER,
    decided_by TEXT NOT NULL DEFAULT '',
    comments TEXT NOT NULL DEFAULT '',
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_approval_journal_run ON approval_journal(run_id, node_id);
CREATE INDEX IF NOT EXISTS idx_approval_journal_status ON approval_journal(status);
"""


class ApprovalJournal:
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else Path.home() / ".fusion-cowork" / "web" / "approval_journal.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info(f"审批日志就绪: {self._path}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record_pending(
        self,
        action_id: str,
        run_id: str,
        node_id: str = "",
        node_name: str = "",
        tool_name: str = "",
        workflow: str = "",
        params: Optional[Dict[str, Any]] = None,
        ts: Optional[float] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO approval_journal "
                "(action_id, run_id, node_id, node_name, tool_name, workflow, params, ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending') "
                "ON CONFLICT(action_id) DO UPDATE SET run_id=excluded.run_id, node_id=excluded.node_id, "
                "node_name=excluded.node_name, tool_name=excluded.tool_name, workflow=excluded.workflow, "
                "params=excluded.params, ts=excluded.ts, status='pending'",
                (
                    str(action_id or ""),
                    str(run_id or ""),
                    str(node_id or ""),
                    str(node_name or ""),
                    str(tool_name or ""),
                    str(workflow or ""),
                    json.dumps(params or {}, ensure_ascii=False),
                    float(ts if ts is not None else time.time()),
                ),
            )
            self._conn.commit()
        logger.info(f"审批日志: pending action={action_id} run={run_id} node={node_id} tool={tool_name}")

    def resolve(self, action_id: str, approved: bool, decided_by: str = "", comments: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approval_journal SET status=?, approved=?, decided_by=?, comments=?, decided_at=? "
                "WHERE action_id=? AND status='pending'",
                (
                    "approved" if approved else "denied",
                    1 if approved else 0,
                    str(decided_by or ""),
                    str(comments or ""),
                    time.time(),
                    str(action_id or ""),
                ),
            )
            self._conn.commit()
            ok = cur.rowcount > 0
        if ok:
            logger.info(f"审批日志: {'approved' if approved else 'denied'} action={action_id} by={decided_by or '-'}")
        return ok

    def resolve_run(self, run_id: str, approved: bool, decided_by: str = "", comments: str = "") -> int:
        # run 级批量落账 — resolve_hitl 无 action_id 上下文时用
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approval_journal SET status=?, approved=?, decided_by=?, comments=?, decided_at=? "
                "WHERE run_id=? AND status='pending'",
                (
                    "approved" if approved else "denied",
                    1 if approved else 0,
                    str(decided_by or ""),
                    str(comments or ""),
                    time.time(),
                    str(run_id or ""),
                ),
            )
            self._conn.commit()
            n = cur.rowcount
        if n:
            logger.info(f"审批日志: run={run_id} {'approved' if approved else 'denied'} x{n} by={decided_by or '-'}")
        return n

    def decision_for(self, run_id: str, node_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM approval_journal WHERE run_id=? AND node_id=? AND status IN ('approved','denied') "
            "ORDER BY decided_at DESC LIMIT 1",
            (str(run_id or ""), str(node_id or "")),
        ).fetchone()
        return dict(row) if row else None

    def pending(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM approval_journal WHERE status='pending' ORDER BY ts ASC").fetchall()
        return [dict(r) for r in rows]

    def pending_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approval_journal WHERE run_id=? AND status='pending' ORDER BY ts ASC",
            (str(run_id or ""),),
        ).fetchall()
        return [dict(r) for r in rows]
