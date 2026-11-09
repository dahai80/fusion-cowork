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
# v2 (issue #223): +space_id 列 — run 归属工作区 (Space), 首页按目录过滤
# v3 (issue #388): +source/labels 列 (归因持久化) + run_summaries 终态摘要表
#   (daemon 重启后 GET /api/runs/{id} 仍可查; 带 90 天保留期)
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
        if version < 2:
            # issue #223: v1 库补列 — 幂等 ALTER (列存在时忽略 OperationalError)
            try:
                self._conn.execute("ALTER TABLE run_states ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        if version < 3:
            # issue #388: v2 库补 source/labels 列 — 幂等 ALTER
            for col in ("source TEXT NOT NULL DEFAULT ''", "labels TEXT NOT NULL DEFAULT '{}'"):
                try:
                    self._conn.execute(f"ALTER TABLE run_states ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
        if version < 4:
            # issue #399: v3 库 run_summaries 补 space_id 列 — 幂等 ALTER
            try:
                self._conn.execute("ALTER TABLE run_summaries ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
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

    # -------------------------------------------------- terminal summaries (issue #388)

    @staticmethod
    def _summary_steps(steps: Any) -> List[Dict[str, Any]]:
        # per-step 摘要: 归因/计费只需要节点标识 + 状态 + 耗时; 丢 input/output 大对象
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
        # 终态摘要 upsert — run 结束时由 RunRegistry 调用 (替代直接 delete);
        # 保留 source/labels + 每步耗时, 供下游 (fusion-commerce) 对账/崩溃恢复
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
        # 最近终态摘要 (finished_at 倒序); 同 load_summary 做 labels/steps 反序列化
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
        # 保留期清理 — 默认 90 天前的终态摘要删除; 返回删除行数
        cutoff = time.time() - max(0, int(retention_days)) * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM run_summaries WHERE finished_at < ?", (cutoff,))
            self._conn.commit()
        if cur.rowcount:
            logger.info(f"终态摘要保留期清理: 删除 {cur.rowcount} 条 (retention={retention_days}d)")
        return int(cur.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
