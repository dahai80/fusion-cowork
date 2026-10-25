# issue #191: 运行控制状态持久化 — paused/resumed/directives/步骤快照落 SQLite。
# daemon 重启 (含 kill -9) 后 RunRegistry.recover_persisted() 走唯一恢复路径:
# 步骤进度 (resume_steps) 与控制状态 (paused/directives) 一并还原, 终不静默丢失。
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fusion_cowork.run_persistence")

# 前向兼容闸门: 读到高于本版本的库直接报错 (loud), 绝不带着不认识的格式静默跑
SCHEMA_VERSION = 1

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
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""

# 只恢复这两态 — 终态行由 registry 在 run 结束时删除, 残留终态行视为脏数据一并清掉
_ACTIVE_STATUSES = ("running", "paused")


class RunStateStore:
    # SQLite 运行状态存储 — 建库/迁移/CRUD; 调用方在 asyncio 单线程内同步调用
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else Path.home() / ".fusion-cowork" / "web" / "run_states.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"运行状态库 {self._path} schema v{version} 高于本版本支持的 v{SCHEMA_VERSION} — "
                f"请升级 fusion-cowork 后再启动 (拒绝静默降级读取)"
            )
        self._conn.executescript(_SCHEMA_SQL)
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()
        logger.info(f"运行状态存储就绪: {self._path} (schema v{SCHEMA_VERSION})")

    def save(
        self,
        record: Dict[str, Any],
        paused_reason: str = "",
    ) -> None:
        # upsert 全量行 — record 由 RunRegistry 持有 (含 workflow_def/steps/directives)
        run_id = str(record.get("run_id", ""))
        if not run_id:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO run_states
                    (run_id, workflow_id, workflow_name, status, paused_reason, workflow_def,
                     steps, directives, memory, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        # 返回 running/paused 行 (dict); 终态残留行清掉并 WARN (loud, 不静默)
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
                logger.warning(f"运行状态库含 {len(stale)} 条终态残留行, 已清理: {stale}")
                self._conn.executemany("DELETE FROM run_states WHERE run_id = ?", [(r,) for r in stale])
                self._conn.commit()
        return rows

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM run_states").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
