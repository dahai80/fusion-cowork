# issue #191: run control-state persistence — paused/resumed/directives/step
# snapshots persisted to SQLite.
# After a daemon restart (including kill -9), RunRegistry.recover_persisted() is the
# single recovery path: step progress (resume_steps) and control state
# (paused/directives) are restored together, so nothing is silently lost.
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fusion_cowork.run_persistence")

# Forward-compatibility gate: reading a DB with a version higher than this one fails
# loudly; never run silently with an unrecognized format
# v2 (issue #223): +space_id column — run belongs to a workspace (Space), home page
# filters by directory
# v3 (issue #388): +source/labels columns (attribution persistence) + run_summaries
#   terminal-summary table (still queryable via GET /api/runs/{id} after daemon
#   restart; 90-day retention)
SCHEMA_VERSION = 4

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run_states (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL DEFAULT '',
    workflow_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    paused_reason TEXT NOT NULL DEFAULT '',
    workflow_def TEXT NOT NULL DEFAULT '{}',
    steps TEXT NOT NULL DEFAULT '[]',
    directives TEXT NOT NULL DEFAULT '[]',
    memory INTEGER NOT NULL DEFAULT 1,
    space_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_summaries (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL DEFAULT '',
    workflow_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT '',
    error TEXT,
    steps TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    space_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_summaries_finished ON run_summaries(finished_at);
"""

# Only these two statuses are recovered — terminal rows are deleted by the registry
# when the run ends; leftover terminal rows are treated as dirty data and cleaned up
_ACTIVE_STATUSES = ("running", "paused")


class RunStateStore:
    # SQLite run-state store — schema creation/migration/CRUD; callers invoke
    # synchronously within the asyncio single thread
    def __init__(self, path: Optional[Path] = None):
        # Default path honors FUSION_COWORK_WEB_DIR (same override as
        # ApprovalJournal) so tests never read a real machine's recovered-run
        # state; unset = legacy ~/.fusion-cowork/web.
        if path:
            self._path = Path(path)
        else:
            web_dir = os.environ.get("FUSION_COWORK_WEB_DIR")
            base = Path(web_dir) if web_dir else Path.home() / ".fusion-cowork" / "web"
            self._path = base / "run_states.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Run-state DB {self._path} schema v{version} is newer than the v{SCHEMA_VERSION} "
                f"supported by this version — upgrade fusion-cowork before starting "
                f"(refusing to silently degrade reads)"
            )
        self._conn.executescript(_SCHEMA_SQL)
        if version < 2:
            # issue #223: add column to v1 DB — idempotent ALTER (ignore OperationalError
            # when the column already exists)
            try:
                self._conn.execute("ALTER TABLE run_states ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        if version < 3:
            # issue #388: add source/labels columns to v2 DB — idempotent ALTER
            for col in ("source TEXT NOT NULL DEFAULT ''", "labels TEXT NOT NULL DEFAULT '{}'"):
                try:
                    self._conn.execute(f"ALTER TABLE run_states ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
        if version < 4:
            # issue #399: add space_id column to run_summaries in v3 DB — idempotent ALTER
            try:
                self._conn.execute("ALTER TABLE run_summaries ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()
        logger.info(f"Run-state store ready: {self._path} (schema v{SCHEMA_VERSION})")

    def save(
        self,
        record: Dict[str, Any],
        paused_reason: str = "",
    ) -> None:
        # Upsert the full row — record is owned by RunRegistry (includes
        # workflow_def/steps/directives)
        run_id = str(record.get("run_id", ""))
        if not run_id:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO run_states
                    (run_id, workflow_id, workflow_name, status, paused_reason, workflow_def,
                     steps, directives, memory, space_id, source, labels, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(record.get("workflow_id", "")),
                    str(record.get("workflow_name", "")),
                    str(record.get("status", "running")),
                    paused_reason,
                    json.dumps(record.get("workflow_def") or {}, ensure_ascii=False),
                    json.dumps(record.get("steps") or [], ensure_ascii=False),
                    json.dumps(record.get("directives") or [], ensure_ascii=False),
                    1 if record.get("memory", True) else 0,
                    str(record.get("space_id") or ""),
                    str(record.get("source") or ""),
                    json.dumps(record.get("labels") or {}, ensure_ascii=False),
                    float(record.get("created_at") or time.time()),
                    time.time(),
                ),
            )
            self._conn.commit()

    def delete(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM run_states WHERE run_id = ?", (run_id,))
            self._conn.commit()

    def load_active(self) -> List[Dict[str, Any]]:
        # Return running/paused rows (dicts); leftover terminal rows are cleaned up
        # with a WARN (loud, never silent)
        rows: List[Dict[str, Any]] = []
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM run_states WHERE status IN (?, ?) ORDER BY created_at",
                _ACTIVE_STATUSES,
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur = self._conn.execute("SELECT run_id, status FROM run_states")
            stale = [r["run_id"] for r in cur.fetchall() if r["status"] not in _ACTIVE_STATUSES]
            if stale:
                logger.warning(f"Run-state DB contains {len(stale)} leftover terminal rows, cleaned: {stale}")
                self._conn.executemany("DELETE FROM run_states WHERE run_id = ?", [(r,) for r in stale])
                self._conn.commit()
        return rows

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM run_states").fetchone()[0])

    # -------------------------------------------------- terminal summaries (issue #388)

    @staticmethod
    def _summary_steps(steps: Any) -> List[Dict[str, Any]]:
        # Per-step summary: attribution/billing only needs node identity + status +
        # timing; large input/output objects are dropped
        out: List[Dict[str, Any]] = []
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            out.append(
                {
                    "node_id": str(s.get("node_id", "")),
                    "node_name": str(s.get("node_name", "")),
                    "status": str(s.get("status", "")),
                    "execution_time": s.get("execution_time"),
                    "attempt": s.get("attempt"),
                }
            )
        return out

    def save_summary(self, record: Dict[str, Any]) -> None:
        # Terminal-summary upsert — called by RunRegistry when the run ends
        # (replacing direct delete); keeps source/labels + per-step timing for
        # downstream (fusion-commerce) reconciliation/crash recovery
        run_id = str(record.get("run_id", ""))
        if not run_id:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO run_summaries
                    (run_id, workflow_id, workflow_name, source, labels, status, error,
                     steps, created_at, started_at, finished_at, space_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(record.get("workflow_id", "")),
                    str(record.get("workflow_name", "")),
                    str(record.get("source") or ""),
                    json.dumps(record.get("labels") or {}, ensure_ascii=False),
                    str(record.get("status") or ""),
                    record.get("error"),
                    json.dumps(self._summary_steps(record.get("steps")), ensure_ascii=False),
                    float(record.get("created_at") or 0.0),
                    float(record.get("started_at") or 0.0),
                    float(record.get("finished_at") or time.time()),
                    str(record.get("space_id") or ""),
                ),
            )
            self._conn.commit()

    def load_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM run_summaries WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["labels"] = json.loads(d.get("labels") or "{}")
        except (TypeError, ValueError):
            d["labels"] = {}
        try:
            d["steps"] = json.loads(d.get("steps") or "[]")
        except (TypeError, ValueError):
            d["steps"] = []
        return d

    def load_summaries(self, limit: int = 100) -> List[Dict[str, Any]]:
        # Most recent terminal summaries (finished_at descending); same labels/steps
        # deserialization as load_summary
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_summaries ORDER BY finished_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            try:
                d["labels"] = json.loads(d.get("labels") or "{}")
            except (TypeError, ValueError):
                d["labels"] = {}
            try:
                d["steps"] = json.loads(d.get("steps") or "[]")
            except (TypeError, ValueError):
                d["steps"] = []
            out.append(d)
        return out

    def prune_summaries(self, retention_days: int = 90) -> int:
        # Retention pruning — delete terminal summaries older than the default 90
        # days; returns the number of deleted rows
        cutoff = time.time() - max(0, int(retention_days)) * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM run_summaries WHERE finished_at < ?", (cutoff,))
            self._conn.commit()
        if cur.rowcount:
            logger.info(f"Terminal-summary retention pruning: deleted {cur.rowcount} rows (retention={retention_days}d)")
        return int(cur.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
