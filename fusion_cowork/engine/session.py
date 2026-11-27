"""Session persistence — storage, recovery, and forking of workflow execution sessions.

Dual backends (v0.4.0):
- SQLite (default): sync sqlite3, for tests/local paths
- Postgres (dsn or env DATABASE_URL, legacy name FUSION_PG_DSN): asyncpg via a
  background loop-thread bridge, keeping the sync interface (DeskRPC sync dispatch
  depends on it)

Supports:
- Session creation and resume
- Session listing and querying
- Session fork — re-execute from a given step
- Automatic cleanup of expired sessions
- Multi-tenant isolation (tenant_id column + query guards, v0.4.0)
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fusion_cowork.db.placeholders import normalize_placeholders
from fusion_cowork.tenant import DEFAULT_TENANT, resolve_tenant_id

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(Path.home() / ".fusion-cowork" / "sessions.db")
SESSION_EXPIRE_DAYS = 30
# issue #221 (DB-3 standard): DATABASE_URL is the single canonical connection name;
# the legacy FUSION_PG_DSN name is still accepted
_PG_DSN_ENV = "DATABASE_URL"
_PG_DSN_ENV_LEGACY = "FUSION_PG_DSN"

_PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL DEFAULT '',
    workflow_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    initial_input TEXT NOT NULL DEFAULT '{}',
    execution_id TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    metadata TEXT NOT NULL DEFAULT '{}',
    steps_snapshot TEXT NOT NULL DEFAULT '[]',
    tenant_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id)
"""


class _AsyncPgBackend:
    """Postgres asyncpg background loop-thread bridge — used by the sync SessionStore.

    Sync callers (DeskRPC dispatch) hold no event loop; this bridge runs a loop in a
    dedicated daemon thread, and sync methods submit via run_coroutine_threadsafe and
    wait for the result. One pool, shared globally.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="session-pg-loop")
        self._thread.start()
        self._pool = self._submit(self._init_pool())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    async def _init_pool(self):
        import asyncpg

        pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            for stmt in _PG_SCHEMA_SQL.strip().split(";"):
                s = stmt.strip()
                if s:
                    await conn.execute(s)
        logger.info("SessionStore postgres pool initialized")
        return pool

    def exec(self, sql: str, params: tuple = ()) -> int:
        pgsql = normalize_placeholders(sql)

        async def _run() -> int:
            async with self._pool.acquire() as conn:
                status = await conn.execute(pgsql, *params)
                parts = status.split()
                return int(parts[-1]) if parts and parts[-1].isdigit() else 0

        return self._submit(_run())

    def fetchone(self, sql: str, params: tuple = ()):
        pgsql = normalize_placeholders(sql)

        async def _run():
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(pgsql, *params)

        return self._submit(_run())

    def fetchall(self, sql: str, params: tuple = ()) -> list:
        pgsql = normalize_placeholders(sql)

        async def _run() -> list:
            async with self._pool.acquire() as conn:
                return list(await conn.fetch(pgsql, *params))

        return self._submit(_run())

    def close(self) -> None:
        try:
            self._submit(self._pool.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


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
    tenant_id: str = DEFAULT_TENANT

    def __post_init__(self):
        if not self.id:
            # HI-13: 128-bit session id (full uuid4 hex) to prevent enumeration of
            # 32/48-bit ids
            self.id = f"sess_{uuid.uuid4().hex}"
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class SessionStore:
    def __init__(self, db_path: Optional[str] = None, dsn: Optional[str] = None):
        self._dsn = dsn or os.environ.get(_PG_DSN_ENV) or os.environ.get(_PG_DSN_ENV_LEGACY)
        self._backend = "postgres" if self._dsn else "sqlite"
        self._db_path = db_path or DEFAULT_DB_PATH
        self._pg: Optional[_AsyncPgBackend] = None
        if self._backend == "sqlite":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()
        else:
            self._init_postgres()

    def _init_sqlite(self) -> None:
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
                    steps_snapshot TEXT NOT NULL DEFAULT '[]',
                    tenant_id TEXT NOT NULL DEFAULT ''
                )
            """)
            # v0.4.0: older DBs lack the tenant_id column -> idempotent ALTER to add it
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "tenant_id" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id)")
        logger.debug(f"SessionStore initialized (sqlite): {self._db_path}")

    def _init_postgres(self) -> None:
        try:
            import asyncpg  # noqa: F401
        except ImportError as e:
            raise RuntimeError("postgres backend requires asyncpg: pip install 'fusion-cowork[cloud]'") from e
        self._pg = _AsyncPgBackend(self._dsn)
        logger.info("SessionStore initialized (postgres pool)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        if self._pg is not None:
            self._pg.close()
            self._pg = None
            logger.info("SessionStore closed (postgres pool)")

    def _row_to_session(self, row: Any) -> Session:
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
            tenant_id=row["tenant_id"],
        )

    def save(self, session: Session, tenant_id: Optional[str] = None) -> Session:
        now = time.time()
        session.updated_at = now
        if not getattr(session, "tenant_id", "") or session.tenant_id == DEFAULT_TENANT:
            session.tenant_id = resolve_tenant_id(tenant_id or getattr(session, "tenant_id", None))
        tid = session.tenant_id
        sql = (
            "INSERT INTO sessions "
            "(id, workflow_id, workflow_name, status, initial_input, "
            "execution_id, created_at, updated_at, completed_at, "
            "metadata, steps_snapshot, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET workflow_id=excluded.workflow_id, "
            "workflow_name=excluded.workflow_name, status=excluded.status, "
            "initial_input=excluded.initial_input, execution_id=excluded.execution_id, "
            "updated_at=excluded.updated_at, completed_at=excluded.completed_at, "
            "metadata=excluded.metadata, steps_snapshot=excluded.steps_snapshot, "
            "tenant_id=excluded.tenant_id"
        )
        params = (
            session.id,
            session.workflow_id,
            session.workflow_name,
            session.status,
            json.dumps(session.initial_input),
            session.execution_id,
            session.created_at,
            session.updated_at,
            session.completed_at,
            json.dumps(session.metadata),
            json.dumps(session.steps_snapshot),
            tid,
        )
        if self._backend == "sqlite":
            with self._connect() as conn:
                conn.execute(sql, params)
        else:
            assert self._pg is not None
            self._pg.exec(sql, params)
        logger.debug(f"Session saved: {session.id} status={session.status} tenant={tid}")
        return session

    def get(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[Session]:
        tid = resolve_tenant_id(tenant_id)
        sql = "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?"
        params = (session_id, tid)
        if self._backend == "sqlite":
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
        else:
            assert self._pg is not None
            row = self._pg.fetchone(sql, params)
        if row:
            return self._row_to_session(row)
        return None

    def list_sessions(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> List[Session]:
        tid = resolve_tenant_id(tenant_id)
        if status:
            sql = "SELECT * FROM sessions WHERE status = ? AND tenant_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params = (status, tid, limit, offset)
        else:
            sql = "SELECT * FROM sessions WHERE tenant_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params = (tid, limit, offset)
        if self._backend == "sqlite":
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        else:
            assert self._pg is not None
            rows = self._pg.fetchall(sql, params)
        return [self._row_to_session(r) for r in rows]

    def update_status(
        self,
        session_id: str,
        status: str,
        completed_at: Optional[float] = None,
        tenant_id: Optional[str] = None,
    ):
        now = time.time()
        tid = resolve_tenant_id(tenant_id)
        if completed_at:
            sql = "UPDATE sessions SET status=?, updated_at=?, completed_at=? WHERE id=? AND tenant_id=?"
            params = (status, now, completed_at, session_id, tid)
        else:
            sql = "UPDATE sessions SET status=?, updated_at=? WHERE id=? AND tenant_id=?"
            params = (status, now, session_id, tid)
        if self._backend == "sqlite":
            with self._connect() as conn:
                conn.execute(sql, params)
        else:
            assert self._pg is not None
            self._pg.exec(sql, params)
        logger.debug(f"Session status updated: {session_id} -> {status} tenant={tid}")

    def update_steps(self, session_id: str, steps: List[Dict[str, Any]], tenant_id: Optional[str] = None):
        now = time.time()
        tid = resolve_tenant_id(tenant_id)
        sql = "UPDATE sessions SET steps_snapshot=?, updated_at=? WHERE id=? AND tenant_id=?"
        params = (json.dumps(steps), now, session_id, tid)
        if self._backend == "sqlite":
            with self._connect() as conn:
                conn.execute(sql, params)
        else:
            assert self._pg is not None
            self._pg.exec(sql, params)

    def fork(self, session_id: str, from_step: int = 0, tenant_id: Optional[str] = None) -> Optional[Session]:
        original = self.get(session_id, tenant_id=tenant_id)
        if not original:
            logger.warning(f"Fork failed: session {session_id} does not exist")
            return None
        forked = Session(
            workflow_id=original.workflow_id,
            workflow_name=original.workflow_name,
            status="forked",
            initial_input=original.initial_input.copy(),
            metadata={"forked_from": session_id, "fork_from_step": from_step},
            tenant_id=original.tenant_id,
        )
        if from_step > 0 and from_step <= len(original.steps_snapshot):
            forked.steps_snapshot = original.steps_snapshot[:from_step]
        self.save(forked)
        logger.info(f"Session fork: {session_id} -> {forked.id} (from_step={from_step}) tenant={forked.tenant_id}")
        return forked

    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        sql = "DELETE FROM sessions WHERE id=? AND tenant_id=?"
        params = (session_id, tid)
        if self._backend == "sqlite":
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
            deleted = cursor.rowcount > 0
        else:
            assert self._pg is not None
            deleted = self._pg.exec(sql, params) > 0
        if deleted:
            logger.debug(f"Session deleted: {session_id} tenant={tid}")
        return deleted

    def resume(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        session = self.get(session_id, tenant_id=tenant_id)
        if not session:
            logger.warning(f"resume failed: session not found {session_id}")
            return None
        if session.status in ("completed", "cancelled"):
            logger.info(f"resume: session already terminal ({session.status}), returning snapshot for replay")
        logger.info(
            f"Session resume: {session_id} status={session.status} "
            f"steps={len(session.steps_snapshot)} tenant={session.tenant_id}"
        )
        return {
            "session": self.to_dict(session),
            "workflow_id": session.workflow_id,
            "workflow_name": session.workflow_name,
            "initial_input": session.initial_input,
            "steps_snapshot": session.steps_snapshot,
            "execution_id": session.execution_id,
        }

    def list_resumable(self, limit: int = 20, tenant_id: Optional[str] = None) -> List[Session]:
        # R-4: older versions included 'running' -> resuming a still-running session
        # means double execution (the same session_id running twice concurrently).
        # Treat running as active and exclude it from the resumable list. Only
        # paused/failed are resumable.
        tid = resolve_tenant_id(tenant_id)
        sql = (
            "SELECT * FROM sessions WHERE status IN ('paused','failed') AND tenant_id = ? "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params = (tid, limit)
        if self._backend == "sqlite":
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        else:
            assert self._pg is not None
            rows = self._pg.fetchall(sql, params)
        logger.info(f"list_resumable: {len(rows)} resumable sessions (running excluded to prevent double execution) tenant={tid}")
        return [self._row_to_session(r) for r in rows]

    def cleanup_expired(self, expire_days: int = SESSION_EXPIRE_DAYS, tenant_id: Optional[str] = None) -> int:
        tid = resolve_tenant_id(tenant_id)
        cutoff = time.time() - expire_days * 86400
        # R-1: paused sessions are also reaped after long inactivity (older versions
        # only cleaned terminal states, so paused leaked forever).
        # Paused uses a shorter threshold: paused beyond 3x expire_days counts as
        # abandoned and is reaped.
        paused_cutoff = time.time() - expire_days * 3 * 86400
        final_sql = (
            "DELETE FROM sessions WHERE updated_at < ? AND status IN ('completed', 'failed', 'cancelled') "
            "AND tenant_id = ?"
        )
        paused_sql = "DELETE FROM sessions WHERE updated_at < ? AND status = 'paused' AND tenant_id = ?"
        if self._backend == "sqlite":
            with self._connect() as conn:
                cursor = conn.execute(final_sql, (cutoff, tid))
                paused_cursor = conn.execute(paused_sql, (paused_cutoff, tid))
            final_count = cursor.rowcount
            paused_count = paused_cursor.rowcount
        else:
            assert self._pg is not None
            final_count = self._pg.exec(final_sql, (cutoff, tid))
            paused_count = self._pg.exec(paused_sql, (paused_cutoff, tid))
        count = final_count + paused_count
        if count:
            logger.info(f"Expired sessions cleaned: {count} (terminal={final_count}, long-paused={paused_count}) tenant={tid}")
        return count

    def to_dict(self, session: Session) -> Dict[str, Any]:
        return asdict(session)
