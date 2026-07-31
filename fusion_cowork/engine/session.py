"""会话持久化 — 工作流执行会话的存储、恢复、分叉。

SQLite 存储会话数据，支持：
- 会话创建与恢复（resume）
- 会话列表与查询
- 会话分叉（fork）— 从某步骤重新执行
- 自动清理过期会话
"""
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(Path.home() / ".fusion-cowork" / "sessions.db")
SESSION_EXPIRE_DAYS = 30


@dataclass
class Session:
    id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    status: str = "created"
    initial_input: Dict[str, Any] = field(default_factory=dict)
    execution_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    steps_snapshot: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class SessionStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL DEFAULT '',
                    workflow_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'created',
                    initial_input TEXT NOT NULL DEFAULT '{}',
                    execution_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    steps_snapshot TEXT NOT NULL DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at)
            """)
        logger.debug(f"SessionStore 初始化完成: {self._db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            workflow_id=row["workflow_id"],
            workflow_name=row["workflow_name"],
            status=row["status"],
            initial_input=json.loads(row["initial_input"]),
            execution_id=row["execution_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            metadata=json.loads(row["metadata"]),
            steps_snapshot=json.loads(row["steps_snapshot"]),
        )

    def save(self, session: Session) -> Session:
        now = time.time()
        session.updated_at = now
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sessions
                (id, workflow_id, workflow_name, status, initial_input,
                 execution_id, created_at, updated_at, completed_at,
                 metadata, steps_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session.id, session.workflow_id, session.workflow_name,
                session.status, json.dumps(session.initial_input),
                session.execution_id, session.created_at, session.updated_at,
                session.completed_at, json.dumps(session.metadata),
                json.dumps(session.steps_snapshot),
            ))
        logger.debug(f"Session 保存: {session.id} status={session.status}")
        return session

    def get(self, session_id: str) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row:
            return self._row_to_session(row)
        return None

    def list_sessions(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Session]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def update_status(self, session_id: str, status: str, completed_at: Optional[float] = None):
        now = time.time()
        with self._connect() as conn:
            if completed_at:
                conn.execute(
                    "UPDATE sessions SET status=?, updated_at=?, completed_at=? WHERE id=?",
                    (status, now, completed_at, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                    (status, now, session_id),
                )
        logger.debug(f"Session 更新状态: {session_id} -> {status}")

    def update_steps(self, session_id: str, steps: List[Dict[str, Any]]):
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET steps_snapshot=?, updated_at=? WHERE id=?",
                (json.dumps(steps), now, session_id),
            )

    def fork(self, session_id: str, from_step: int = 0) -> Optional[Session]:
        original = self.get(session_id)
        if not original:
            logger.warning(f"Fork 失败: Session {session_id} 不存在")
            return None
        forked = Session(
            workflow_id=original.workflow_id,
            workflow_name=original.workflow_name,
            status="forked",
            initial_input=original.initial_input.copy(),
            metadata={"forked_from": session_id, "fork_from_step": from_step},
        )
        if from_step > 0 and from_step <= len(original.steps_snapshot):
            forked.steps_snapshot = original.steps_snapshot[:from_step]
        self.save(forked)
        logger.info(f"Session fork: {session_id} -> {forked.id} (from_step={from_step})")
        return forked

    def delete(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug(f"Session 删除: {session_id}")
        return deleted

    def cleanup_expired(self, expire_days: int = SESSION_EXPIRE_DAYS) -> int:
        cutoff = time.time() - expire_days * 86400
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE updated_at < ? AND status IN ('completed', 'failed', 'cancelled')",
                (cutoff,),
            )
        count = cursor.rowcount
        if count:
            logger.info(f"清理过期 Session: {count} 条")
        return count

    def to_dict(self, session: Session) -> Dict[str, Any]:
        return asdict(session)
