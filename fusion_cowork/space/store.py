"""Collaborative space persistence store — dual backend (v0.4.0 Stage 3).

14 tables: spaces, space_members, space_messages, space_comments,
           space_agents, space_snapshots, space_invite_links,
           space_workflows, space_artifacts, sync_events,
           sidebar_modules, space_notifications (+ schema_migrations).

Backend selection (Option C single class):
- dsn or env DATABASE_URL given (issue #221, spec DB-3; legacy name FUSION_PG_DSN still accepted) → postgres (asyncpg pool, RLS defense-in-depth)
- only data_dir given → sqlite (aiosqlite WAL, test/local stand-in)

All SQL uses ? placeholders (SQLite style); the postgres path normalizes ?→$1,$2 automatically via _DbHandle.exec.
946 tests unchanged (still pass data_dir, selecting sqlite).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite

from fusion_cowork.db import apply_rls, set_tenant_context
from fusion_cowork.db.migrations import MigrationRunner
from fusion_cowork.db.placeholders import normalize_placeholders
from fusion_cowork.tenant import resolve_tenant_id

from .models import (
    Space,
    SpaceConfig,
    SpaceMember,
    SpaceMessage,
    SpaceRole,
    SpaceSnapshot,
    SpaceStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = os.path.expanduser("~/.fusion-cowork/spaces")
_DB_FILENAME = "spaces.db"
# issue #221 (spec DB-3): DATABASE_URL is the single canonical connection name; legacy name FUSION_PG_DSN still accepted
_PG_DSN_ENV = "DATABASE_URL"
_PG_DSN_ENV_LEGACY = "FUSION_PG_DSN"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    kb_bind_mode TEXT NOT NULL DEFAULT 'new_private',
    kb_id TEXT,
    collab_mode TEXT NOT NULL DEFAULT 'local',
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_members (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    display_name TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS space_messages (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'text',
    attachments TEXT NOT NULL DEFAULT '[]',
    parent_msg_id TEXT,
    thread_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_comments (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    author_id TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_agents (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    agent_type TEXT NOT NULL DEFAULT 'assistant',
    system_prompt TEXT NOT NULL DEFAULT '',
    enable_rag INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_snapshots (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    messages_count INTEGER NOT NULL DEFAULT 0,
    agents_count INTEGER NOT NULL DEFAULT 0,
    files_count INTEGER NOT NULL DEFAULT 0,
    workflows_count INTEGER NOT NULL DEFAULT 0,
    artifacts_count INTEGER NOT NULL DEFAULT 0,
    snapshot_data TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_invite_links (
    code TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    max_uses INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_workflows (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    workflow_data TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_artifacts (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT 'file',
    content TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    event_data TEXT NOT NULL DEFAULT '{}',
    lamport_ts INTEGER NOT NULL DEFAULT 0,
    node_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    resource TEXT NOT NULL DEFAULT '',
    detail_hash TEXT NOT NULL DEFAULT '',
    prev_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    entry_hash TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_space_members_space ON space_members(space_id);
CREATE INDEX IF NOT EXISTS idx_space_messages_space ON space_messages(space_id);
CREATE INDEX IF NOT EXISTS idx_space_messages_created ON space_messages(space_id, created_at);
CREATE INDEX IF NOT EXISTS idx_space_agents_space ON space_agents(space_id);
CREATE INDEX IF NOT EXISTS idx_space_snapshots_space ON space_snapshots(space_id);
CREATE INDEX IF NOT EXISTS idx_space_workflows_space ON space_workflows(space_id);
CREATE INDEX IF NOT EXISTS idx_space_artifacts_space ON space_artifacts(space_id);
CREATE INDEX IF NOT EXISTS idx_sync_events_space ON sync_events(space_id);
"""

# #122: tenant indexes must not go into _SCHEMA_SQL — old databases lack the tenant_id column,
# executescript would die immediately with `no such column: tenant_id` and MigrationRunner v1
# (ADD COLUMN) would never get a chance to run. These 4 indexes run after MigrationRunner.run_pending()
# (v1 has added the columns; IF NOT EXISTS keeps it idempotent).
_TENANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_space_members_tenant ON space_members(tenant_id);
CREATE INDEX IF NOT EXISTS idx_space_messages_tenant ON space_messages(tenant_id);
CREATE INDEX IF NOT EXISTS idx_spaces_tenant ON spaces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON audit_log(tenant_id, created_at);
"""


def _pg_schema_sql() -> str:
    """Postgres dialect variant — swaps SQLite-specific syntax for Postgres-compatible forms.

    - INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
    The remaining TEXT/INTEGER/CREATE INDEX IF NOT EXISTS are compatible on both sides.
    """
    return _SCHEMA_SQL.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY")


class _ExecResult:
    """Write result — rowcount (affected rows) + lastrowid (sqlite AUTOINCREMENT id)."""

    __slots__ = ("lastrowid", "rowcount")

    def __init__(self, rowcount: int = 0, lastrowid: Optional[int] = None):
        self.rowcount = rowcount
        self.lastrowid = lastrowid


def _parse_pg_rowcount(status: str) -> int:
    """Parses the command tag returned by asyncpg execute() into affected row count.

    "DELETE 3" → 3; "INSERT 0 1" → 1; "UPDATE 2" → 2; "BEGIN"/"COMMIT" → 0.
    """
    if not status:
        return 0
    parts = status.split()
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


class _DbHandle:
    """Unified DB handle — works across sqlite/postgres.

    SQL keeps ? placeholders (SQLite style); the postgres path normalizes them to $1,$2 automatically.
    - exec: write, returns _ExecResult (rowcount/lastrowid)
    - fetchone/fetchall/fetchval: read, returns row/list/scalar
    Row access row["col"] is compatible on both backends (aiosqlite.Row + asyncpg.Record).
    """

    def __init__(self, backend: str, conn: Any, owned: bool, pool: Any = None):
        self._backend = backend
        self._conn = conn
        self._owned = owned  # postgres read/tx: return conn to pool after use; sqlite: shared conn, not returned
        self._pool = pool  # postgres: used to return the conn

    async def exec(self, sql: str, params: Any = ()) -> _ExecResult:
        if self._backend == "sqlite":
            cursor = await self._conn.execute(sql, params)
            return _ExecResult(cursor.rowcount, cursor.lastrowid)
        pgsql = normalize_placeholders(sql)
        status = await self._conn.execute(pgsql, *params)
        return _ExecResult(_parse_pg_rowcount(status), None)

    async def fetchone(self, sql: str, params: Any = ()):
        if self._backend == "sqlite":
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchone()
        pgsql = normalize_placeholders(sql)
        return await self._conn.fetchrow(pgsql, *params)

    async def fetchall(self, sql: str, params: Any = ()) -> list:
        if self._backend == "sqlite":
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchall()
        pgsql = normalize_placeholders(sql)
        return list(await self._conn.fetch(pgsql, *params))

    async def fetchval(self, sql: str, params: Any = ()):
        if self._backend == "sqlite":
            cursor = await self._conn.execute(sql, params)
            row = await cursor.fetchone()
            return row[0] if row else None
        pgsql = normalize_placeholders(sql)
        return await self._conn.fetchval(pgsql, *params)

    async def commit(self) -> None:
        if self._backend == "sqlite":
            await self._conn.commit()
        else:
            await self._conn.execute("COMMIT")

    async def rollback(self) -> None:
        if self._backend == "sqlite":
            await self._conn.execute("ROLLBACK")
        else:
            await self._conn.execute("ROLLBACK")

    async def close(self) -> None:
        # postgres returns the conn to the pool; sqlite shared conn is not closed
        if self._owned and self._backend == "postgres" and self._pool is not None:
            try:
                await self._pool.release(self._conn)
            except Exception as e:
                logger.warning(f"postgres conn release failed: {e}")


def _sqlite_executor(db: aiosqlite.Connection):
    """MigrationRunner executor — sqlite (aiosqlite conn)."""

    async def _exec(sql: str, params: Any = (), read: bool = False):
        cursor = await db.execute(sql, params or ())
        if read:
            return await cursor.fetchall()
        await db.commit()
        return None

    return _exec


def _pg_executor(conn: Any):
    """MigrationRunner executor — postgres (asyncpg conn, inside a transaction)."""

    async def _exec(sql: str, params: Any = (), read: bool = False):
        pgsql = normalize_placeholders(sql)
        if read:
            return list(await conn.fetch(pgsql, *(params or ())))
        await conn.execute(pgsql, *(params or ()))
        return None

    return _exec


class SpaceStore:
    """Collaborative space store — dual-backend (sqlite/postgres) async CRUD.

    Pass a dsn (or env DATABASE_URL, legacy name FUSION_PG_DSN) → postgres asyncpg pool; pass only data_dir → sqlite aiosqlite.
    """

    def __init__(
        self,
        data_dir: str = _DEFAULT_DATA_DIR,
        trajectory_exporter=None,
        dsn: Optional[str] = None,
    ):
        self._dsn = dsn or os.environ.get(_PG_DSN_ENV) or os.environ.get(_PG_DSN_ENV_LEGACY)
        self._backend = "postgres" if self._dsn else "sqlite"
        self._data_dir = data_dir
        self._db_path = os.path.join(data_dir, _DB_FILENAME)
        self._db: Optional[aiosqlite.Connection] = None
        self._pool = None  # asyncpg.Pool (postgres)
        self._trajectory_exporter = trajectory_exporter
        # A-8: sqlite write-transaction serialization lock — with a single shared connection, concurrent writes always hit OperationalError: database is locked.
        # asyncio.Lock serializes writes, paired with busy_timeout retries, no more bare commits. Postgres uses pool+MVCC, so this lock is a no-op there.
        self._write_lock = asyncio.Lock()
        # issue #463: concurrent _ensure_db triggers used to double-run initialize — the second executescript
        # would hit 'no such table' (under WAL the other connection has not yet seen the uncommitted schema). Lock + idempotent sentinel.
        self._init_lock = asyncio.Lock()
        self._initialized = False

    @property
    def backend(self) -> str:
        return self._backend

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._initialize_inner()
            self._initialized = True

    async def _initialize_inner(self) -> None:
        if self._backend == "postgres":
            await self._init_postgres()
        else:
            await self._init_sqlite()

    async def _init_sqlite(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # A-8: busy_timeout 5s — wait on write conflicts instead of failing immediately with locked.
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA_SQL)
        # Versioned migrations (replaces the old blind ALTER try/except)
        runner = MigrationRunner(
            "sqlite",
            executor=_sqlite_executor(self._db),
            error_class=aiosqlite.OperationalError,
        )
        await runner.run_pending()
        # #122: tenant indexes must be created after v1 (ADD COLUMN tenant_id)
        await self._db.executescript(_TENANT_INDEX_SQL)
        await self._db.commit()
        logger.info(f"SpaceStore initialization complete (sqlite): {self._db_path}")

    async def _init_postgres(self) -> None:
        try:
            import asyncpg
        except ImportError as e:
            raise RuntimeError("postgres backend requires asyncpg: pip install 'fusion-cowork[cloud]'") from e
        import asyncpg

        self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(_pg_schema_sql())
            runner = MigrationRunner(
                "postgres",
                executor=_pg_executor(conn),
                error_class=asyncpg.DuplicateColumnError,
            )
            await runner.run_pending()
            # #122: tenant indexes must be created after v1 (ADD COLUMN tenant_id)
            for stmt in filter(None, (s.strip() for s in _TENANT_INDEX_SQL.split(";"))):
                await conn.execute(stmt)
            # RLS defense-in-depth (postgres only): still blocks cross-tenant access when SQL misses a tenant_id guard
            await apply_rls(conn)
        logger.info("SpaceStore initialization complete (postgres): pool min=2 max=10")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("SpaceStore closed (sqlite)")
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("SpaceStore closed (postgres pool)")

    async def _ensure_db(self) -> Any:
        if self._db is None and self._backend == "sqlite":
            await self.initialize()
        return self._db

    async def _read_handle(self) -> _DbHandle:
        """Read-path handle — sqlite uses the shared conn; postgres takes one conn from the pool (returned after use)."""
        if self._backend == "sqlite":
            db = await self._ensure_db()
            return _DbHandle(self._backend, db, owned=False)
        # postgres: the caller must await handle.close() to return the conn after use
        conn = await self._pool.acquire()
        return _DbHandle(self._backend, conn, owned=True, pool=self._pool)

    # A-8: write transaction context — sqlite serial lock + BEGIN IMMEDIATE; postgres pool.acquire + BEGIN.
    # All write methods (including the artifact service) go through this context, eliminating bare commits.
    class _WriteTx:
        def __init__(self, store: SpaceStore, tenant_id: Optional[str] = None):
            self._store = store
            self._tenant_id = tenant_id
            self.handle: Optional[_DbHandle] = None

        async def __aenter__(self) -> _DbHandle:
            store = self._store
            if store._backend == "sqlite":
                await store._write_lock.acquire()
                db = await store._ensure_db()
                await db.execute("BEGIN IMMEDIATE")
                self.handle = _DbHandle("sqlite", db, owned=False)
            else:
                conn = await store._pool.acquire()
                await conn.execute("BEGIN")
                # RLS: set this transaction's tenant context; policies filter on it
                if self._tenant_id is not None:
                    await set_tenant_context(conn, self._tenant_id)
                self.handle = _DbHandle("postgres", conn, owned=True, pool=store._pool)
            return self.handle

        async def __aexit__(self, exc_type, exc, tb):
            h = self.handle
            assert h is not None
            try:
                if exc_type is None:
                    await h.commit()
                else:
                    await h.rollback()
            except Exception as ce:
                logger.error(f"_WriteTx finalization error: {ce}")
            finally:
                await h.close()
                if self._store._backend == "sqlite":
                    self._store._write_lock.release()
            return False

    def write_tx(self, tenant_id: Optional[str] = None) -> _WriteTx:
        # Public entry point — cross-module writes (artifact/service, etc.) share the same serial transaction for write isolation.
        return self._WriteTx(self, tenant_id=tenant_id)

    # ── Read helpers — sqlite uses the shared conn; postgres takes from the pool and returns it ──

    async def _fetchone(self, sql: str, params: Any = ()) -> Any:
        h = await self._read_handle()
        try:
            return await h.fetchone(sql, params)
        finally:
            await h.close()

    async def _fetchall(self, sql: str, params: Any = ()) -> list:
        h = await self._read_handle()
        try:
            return await h.fetchall(sql, params)
        finally:
            await h.close()

    async def _fetchval(self, sql: str, params: Any = ()) -> Any:
        h = await self._read_handle()
        try:
            return await h.fetchval(sql, params)
        finally:
            await h.close()

    # ── Space CRUD ──

    async def create_space(self, space: Space, tenant_id: Optional[str] = None) -> Space:
        tid = resolve_tenant_id(tenant_id or getattr(space, "tenant_id", None))
        # Always overwrite space.tenant_id with the resolved tid — prevents the case where the caller
        # does not set it explicitly (default DEFAULT_TENANT) but passes a tenant_id argument, leaving the
        # space object carrying the old default and sending downstream (add_member etc.) to the wrong tenant.
        space.tenant_id = tid
        # Stage 7: per-tenant quota check (opt-in; no quota configured → unlimited pass-through)
        from ..security.quotas import get_default_quota_enforcer

        try:
            cur = await self._fetchval("SELECT COUNT(*) FROM spaces WHERE tenant_id = ?", (tid,))
            get_default_quota_enforcer().check_create_space(tid, int(cur or 0))
        except Exception as e:
            if "QuotaExceeded" in type(e).__name__:
                raise
            logger.debug(f"quota check skipped: {e}")
        async with self.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO spaces (id, name, description, owner_id, status, "
                "kb_bind_mode, kb_id, collab_mode, config, created_at, updated_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    space.id,
                    space.name,
                    space.description,
                    space.owner_id,
                    space.status.value if isinstance(space.status, SpaceStatus) else space.status,
                    space.kb_bind_mode,
                    space.kb_id,
                    space.collab_mode,
                    json.dumps(space.config.to_dict(), ensure_ascii=False),
                    space.created_at,
                    space.updated_at,
                    tid,
                ),
            )
        logger.info(f"SpaceStore.create_space id={space.id} name={space.name} tenant={tid}")
        return space

    async def get_space(self, space_id: str, tenant_id: Optional[str] = None) -> Optional[Space]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._fetchone("SELECT * FROM spaces WHERE id = ? AND tenant_id = ?", (space_id, tid))
        if not row:
            return None
        return self._row_to_space(row)

    async def list_spaces(
        self,
        status: Optional[str] = None,
        owner_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> List[Space]:
        tid = resolve_tenant_id(tenant_id)
        conditions = ["tenant_id = ?"]
        params: list = [tid]
        if status:
            conditions.append("status = ?")
            params.append(status)
        if owner_id:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        where = f" WHERE {' AND '.join(conditions)}"
        params.extend([limit, offset])
        rows = await self._fetchall(
            f"SELECT * FROM spaces{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_space(r) for r in rows]

    async def update_space(self, space_id: str, tenant_id: Optional[str] = None, **kwargs) -> Optional[Space]:
        tid = resolve_tenant_id(tenant_id)
        # CR-4: column whitelist, rejects non-whitelisted columns (prevents kwargs interpolation injecting unknown column names)
        _ALLOWED = {"name", "description", "owner_id", "status", "kb_bind_mode", "kb_id", "collab_mode", "config"}
        sets = []
        params: list = []
        for key, val in kwargs.items():
            if key not in _ALLOWED:
                logger.warning(f"SpaceStore.update_space rejected non-whitelisted column: {key}")
                continue
            if key == "config":
                sets.append("config = ?")
                params.append(json.dumps(val.to_dict() if hasattr(val, "to_dict") else val, ensure_ascii=False))
            else:
                sets.append(f"{key} = ?")
                params.append(val)
        if not sets:
            return await self.get_space(space_id, tid)
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.extend([space_id, tid])
        async with self.write_tx(tid) as h:
            await h.exec(f"UPDATE spaces SET {', '.join(sets)} WHERE id = ? AND tenant_id = ?", params)
        logger.info(f"SpaceStore.update_space id={space_id} fields={list(kwargs.keys())} tenant={tid}")
        return await self.get_space(space_id, tid)

    async def delete_space(self, space_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            await h.exec("DELETE FROM space_members WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_messages WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_agents WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_snapshots WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_workflows WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_artifacts WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            await h.exec("DELETE FROM space_invite_links WHERE space_id = ? AND tenant_id = ?", (space_id, tid))
            res = await h.exec("DELETE FROM spaces WHERE id = ? AND tenant_id = ?", (space_id, tid))
            deleted = res.rowcount > 0
        logger.info(f"SpaceStore.delete_space id={space_id} deleted={deleted} tenant={tid}")
        return deleted

    # ── Member CRUD ──

    async def add_member(self, member: SpaceMember, tenant_id: Optional[str] = None) -> SpaceMember:
        tid = resolve_tenant_id(tenant_id or getattr(member, "tenant_id", None))
        if not getattr(member, "tenant_id", ""):
            member.tenant_id = tid
        async with self.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_members (space_id, user_id, role, display_name, joined_at, last_active, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    member.space_id,
                    member.user_id,
                    member.role.value if isinstance(member.role, SpaceRole) else member.role,
                    member.display_name,
                    member.joined_at,
                    member.last_active,
                    tid,
                ),
            )
        logger.info(
            f"SpaceStore.add_member space={member.space_id} user={member.user_id} role={member.role} tenant={tid}"
        )
        return member

    async def add_member_checked(
        self, member: SpaceMember, max_members: int, tenant_id: Optional[str] = None
    ) -> SpaceMember:
        """P2-7 (audit 0912): atomic capacity-checked member insert.

        The COUNT guard and the INSERT run in the same serial write
        transaction — previously they were separate calls, so concurrent
        joins could exceed max_members."""
        tid = resolve_tenant_id(tenant_id or getattr(member, "tenant_id", None))
        if not getattr(member, "tenant_id", ""):
            member.tenant_id = tid
        async with self.write_tx(tid) as h:
            cur = await h.fetchval(
                "SELECT COUNT(*) FROM space_members WHERE space_id = ? AND tenant_id = ?",
                (member.space_id, tid),
            )
            if max_members > 0 and int(cur or 0) >= max_members:
                raise ValueError(f"space {member.space_id} is full ({max_members} members)")
            await h.exec(
                "INSERT INTO space_members (space_id, user_id, role, display_name, joined_at, last_active, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    member.space_id,
                    member.user_id,
                    member.role.value if isinstance(member.role, SpaceRole) else member.role,
                    member.display_name,
                    member.joined_at,
                    member.last_active,
                    tid,
                ),
            )
        logger.info(
            f"SpaceStore.add_member_checked space={member.space_id} user={member.user_id} "
            f"role={member.role} count={int(cur or 0) + 1} tenant={tid}"
        )
        return member

    async def get_member(self, space_id: str, user_id: str, tenant_id: Optional[str] = None) -> Optional[SpaceMember]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._fetchone(
            "SELECT * FROM space_members WHERE space_id = ? AND user_id = ? AND tenant_id = ?",
            (space_id, user_id, tid),
        )
        if not row:
            return None
        return self._row_to_member(row)

    async def list_members(self, space_id: str, tenant_id: Optional[str] = None) -> List[SpaceMember]:
        tid = resolve_tenant_id(tenant_id)
        rows = await self._fetchall(
            "SELECT * FROM space_members WHERE space_id = ? AND tenant_id = ? ORDER BY joined_at",
            (space_id, tid),
        )
        return [self._row_to_member(r) for r in rows]

    async def update_member(
        self, space_id: str, user_id: str, tenant_id: Optional[str] = None, **kwargs
    ) -> Optional[SpaceMember]:
        tid = resolve_tenant_id(tenant_id)
        # CR-4: column whitelist
        _ALLOWED = {"role", "display_name", "last_active"}
        sets = []
        params: list = []
        for key, val in kwargs.items():
            if key not in _ALLOWED:
                logger.warning(f"SpaceStore.update_member rejected non-whitelisted column: {key}")
                continue
            if isinstance(val, SpaceRole):
                val = val.value
            sets.append(f"{key} = ?")
            params.append(val)
        if not sets:
            return await self.get_member(space_id, user_id, tid)
        sets.append("last_active = ?")
        params.append(datetime.now().isoformat())
        params.extend([space_id, user_id, tid])
        async with self.write_tx(tid) as h:
            await h.exec(
                f"UPDATE space_members SET {', '.join(sets)} WHERE space_id = ? AND user_id = ? AND tenant_id = ?",
                params,
            )
        logger.info(f"SpaceStore.update_member space={space_id} user={user_id} tenant={tid}")
        return await self.get_member(space_id, user_id, tid)

    async def remove_member(self, space_id: str, user_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            res = await h.exec(
                "DELETE FROM space_members WHERE space_id = ? AND user_id = ? AND tenant_id = ?",
                (space_id, user_id, tid),
            )
            deleted = res.rowcount > 0
        logger.info(f"SpaceStore.remove_member space={space_id} user={user_id} deleted={deleted} tenant={tid}")
        return deleted

    async def count_members(self, space_id: str, tenant_id: Optional[str] = None) -> int:
        tid = resolve_tenant_id(tenant_id)
        val = await self._fetchval(
            "SELECT COUNT(*) FROM space_members WHERE space_id = ? AND tenant_id = ?",
            (space_id, tid),
        )
        return val or 0

    # ── Message CRUD ──

    async def add_message(self, msg: SpaceMessage, tenant_id: Optional[str] = None) -> SpaceMessage:
        tid = resolve_tenant_id(tenant_id or getattr(msg, "tenant_id", None))
        if not getattr(msg, "tenant_id", ""):
            msg.tenant_id = tid
        # Stage 7 / P2-4 (audit 0912): quota COUNT + check moved INSIDE the
        # serial write transaction — previously COUNT ran outside, so
        # concurrent inserts could overshoot the quota.
        from ..security.quotas import get_default_quota_enforcer

        async with self.write_tx(tid) as h:
            cur = await h.fetchval(
                "SELECT COUNT(*) FROM space_messages WHERE space_id = ? AND tenant_id = ?",
                (msg.space_id, tid),
            )
            try:
                get_default_quota_enforcer().check_add_message(tid, msg.space_id, int(cur or 0))
            except Exception as e:
                if "QuotaExceeded" in type(e).__name__:
                    raise
                logger.debug(f"quota check skipped: {e}")
            await h.exec(
                "INSERT INTO space_messages (id, space_id, user_id, agent_id, role, content, "
                "content_type, attachments, parent_msg_id, thread_id, metadata, created_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.id,
                    msg.space_id,
                    msg.user_id,
                    msg.agent_id,
                    msg.role,
                    msg.content,
                    msg.content_type,
                    json.dumps(msg.attachments, ensure_ascii=False),
                    msg.parent_msg_id,
                    msg.thread_id,
                    json.dumps(msg.metadata, ensure_ascii=False),
                    msg.created_at,
                    tid,
                ),
            )
        logger.debug(f"SpaceStore.add_message id={msg.id} space={msg.space_id} tenant={tid}")
        if self._trajectory_exporter is not None:
            try:
                self._trajectory_exporter.export_message(msg)
            except Exception as e:
                logger.error(f"space trajectory export failed msg={msg.id}: {e}")
        return msg

    async def get_messages(
        self,
        space_id: str,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> List[SpaceMessage]:
        tid = resolve_tenant_id(tenant_id)
        rows = await self._fetchall(
            "SELECT * FROM space_messages WHERE space_id = ? AND tenant_id = ? "
            "ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (space_id, tid, limit, offset),
        )
        return [self._row_to_message(r) for r in rows]

    async def delete_message(self, msg_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            res = await h.exec(
                "DELETE FROM space_messages WHERE id = ? AND tenant_id = ?",
                (msg_id, tid),
            )
            return res.rowcount > 0

    async def count_messages(self, space_id: str, tenant_id: Optional[str] = None) -> int:
        tid = resolve_tenant_id(tenant_id)
        val = await self._fetchval(
            "SELECT COUNT(*) FROM space_messages WHERE space_id = ? AND tenant_id = ?",
            (space_id, tid),
        )
        return val or 0

    # ── Agent CRUD ──

    async def add_agent(self, agent_data: Dict[str, Any], tenant_id: Optional[str] = None) -> str:
        import uuid

        tid = resolve_tenant_id(tenant_id or agent_data.get("tenant_id"))
        agent_id = agent_data.get("id") or f"agent_{uuid.uuid4().hex[:8]}"
        # Stage 7: per-tenant agent quota (opt-in, unlimited by default)
        from ..security.quotas import get_default_quota_enforcer

        try:
            cur = await self._fetchval(
                "SELECT COUNT(*) FROM space_agents WHERE space_id = ? AND tenant_id = ?",
                (agent_data.get("space_id", ""), tid),
            )
            get_default_quota_enforcer().check_create_agent(tid, agent_data.get("space_id", ""), int(cur or 0))
        except Exception as e:
            if "QuotaExceeded" in type(e).__name__:
                raise
            logger.debug(f"quota check skipped: {e}")
        async with self.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_agents (id, space_id, name, agent_type, system_prompt, "
                "enable_rag, config, created_by, created_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    agent_data.get("space_id", ""),
                    agent_data.get("name", ""),
                    agent_data.get("agent_type", "assistant"),
                    agent_data.get("system_prompt", ""),
                    int(agent_data.get("enable_rag", False)),
                    json.dumps(agent_data.get("config", {}), ensure_ascii=False),
                    agent_data.get("created_by", ""),
                    datetime.now().isoformat(),
                    tid,
                ),
            )
        logger.info(f"SpaceStore.add_agent id={agent_id} tenant={tid}")
        return agent_id

    async def get_agent_def(
        self, space_id: str, agent_id: str, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._fetchone(
            "SELECT * FROM space_agents WHERE id = ? AND space_id = ? AND tenant_id = ?",
            (agent_id, space_id, tid),
        )
        if not row:
            return None
        return dict(row)

    async def list_agents(self, space_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        rows = await self._fetchall(
            "SELECT * FROM space_agents WHERE space_id = ? AND tenant_id = ?",
            (space_id, tid),
        )
        return [dict(r) for r in rows]

    async def update_agent(
        self,
        space_id: str,
        agent_id: str,
        tenant_id: Optional[str] = None,
        **kwargs,
    ) -> bool:
        if not kwargs:
            return False
        tid = resolve_tenant_id(tenant_id)
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k in ("name", "agent_type", "system_prompt", "enable_rag", "config"):
                if k == "config":
                    v = json.dumps(v, ensure_ascii=False)
                elif k == "enable_rag":
                    v = int(v)
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return False
        vals.extend([agent_id, space_id, tid])
        async with self.write_tx(tid) as h:
            await h.exec(
                f"UPDATE space_agents SET {', '.join(sets)} WHERE id = ? AND space_id = ? AND tenant_id = ?",
                vals,
            )
        logger.info(f"SpaceStore.update_agent id={agent_id} fields={list(kwargs.keys())} tenant={tid}")
        return True

    async def remove_agent(self, space_id: str, agent_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            await h.exec(
                "DELETE FROM space_agents WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (agent_id, space_id, tid),
            )
        logger.info(f"SpaceStore.remove_agent id={agent_id} space={space_id} tenant={tid}")
        return True

    # ── Snapshot CRUD ──

    async def create_snapshot(self, snapshot: SpaceSnapshot, tenant_id: Optional[str] = None) -> SpaceSnapshot:
        tid = resolve_tenant_id(tenant_id or getattr(snapshot, "tenant_id", None))
        if not getattr(snapshot, "tenant_id", ""):
            snapshot.tenant_id = tid
        async with self.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_snapshots (id, space_id, name, messages_count, agents_count, "
                "files_count, workflows_count, artifacts_count, snapshot_data, created_by, "
                "created_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.space_id,
                    snapshot.name,
                    snapshot.messages_count,
                    snapshot.agents_count,
                    snapshot.files_count,
                    snapshot.workflows_count,
                    snapshot.artifacts_count,
                    json.dumps(snapshot.snapshot_data, ensure_ascii=False),
                    snapshot.created_by,
                    snapshot.created_at,
                    tid,
                ),
            )
        logger.info(f"SpaceStore.create_snapshot id={snapshot.id} space={snapshot.space_id} tenant={tid}")
        return snapshot

    async def list_snapshots(self, space_id: str, tenant_id: Optional[str] = None) -> List[SpaceSnapshot]:
        tid = resolve_tenant_id(tenant_id)
        rows = await self._fetchall(
            "SELECT * FROM space_snapshots WHERE space_id = ? AND tenant_id = ? ORDER BY created_at DESC",
            (space_id, tid),
        )
        return [self._row_to_snapshot(r) for r in rows]

    async def get_snapshot(
        self, space_id: str, snapshot_id: str, tenant_id: Optional[str] = None
    ) -> Optional[SpaceSnapshot]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._fetchone(
            "SELECT * FROM space_snapshots WHERE id = ? AND space_id = ? AND tenant_id = ?",
            (snapshot_id, space_id, tid),
        )
        return self._row_to_snapshot(row) if row else None

    async def delete_snapshot(self, space_id: str, snapshot_id: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            await h.exec(
                "DELETE FROM space_snapshots WHERE id = ? AND space_id = ? AND tenant_id = ?",
                (snapshot_id, space_id, tid),
            )
        logger.info(f"SpaceStore.delete_snapshot id={snapshot_id} tenant={tid}")
        return True

    # ── Comment CRUD ──

    async def add_comment(
        self,
        message_id: str,
        author_id: str,
        author_name: str,
        content: str,
        space_id: str = "",
        thread_id: str = "",
        tenant_id: Optional[str] = None,
    ) -> str:
        import uuid

        tid = resolve_tenant_id(tenant_id)
        comment_id = f"cmt_{uuid.uuid4().hex[:8]}"
        async with self.write_tx(tid) as h:
            await h.exec(
                "INSERT INTO space_comments (id, space_id, message_id, author_id, author_name, "
                "content, thread_id, created_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comment_id,
                    space_id,
                    message_id,
                    author_id,
                    author_name,
                    content,
                    thread_id,
                    datetime.now().isoformat(),
                    tid,
                ),
            )
        logger.info(f"SpaceStore.add_comment id={comment_id} thread={thread_id or '-'} tenant={tid}")
        return comment_id

    async def list_comments(self, message_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        rows = await self._fetchall(
            "SELECT * FROM space_comments WHERE message_id = ? AND tenant_id = ? ORDER BY created_at",
            (message_id, tid),
        )
        return [dict(r) for r in rows]

    # ── Invite Link CRUD ──

    async def create_invite(
        self,
        code: str,
        space_id: str,
        role: str = "member",
        max_uses: int = 0,
        expires_at: Optional[str] = None,
        created_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> str:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            role_str = role.value if isinstance(role, SpaceRole) else role
            await h.exec(
                "INSERT INTO space_invite_links (code, space_id, role, max_uses, uses, "
                "expires_at, created_by, created_at, tenant_id) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (code, space_id, role_str, max_uses, expires_at, created_by, datetime.now().isoformat(), tid),
            )
        logger.info(f"SpaceStore.create_invite code={code} space={space_id} tenant={tid}")
        return code

    async def get_invite(self, code: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        tid = resolve_tenant_id(tenant_id)
        row = await self._fetchone(
            "SELECT * FROM space_invite_links WHERE code = ? AND tenant_id = ?",
            (code, tid),
        )
        return dict(row) if row else None

    async def use_invite(self, code: str, tenant_id: Optional[str] = None) -> bool:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            await h.exec(
                "UPDATE space_invite_links SET uses = uses + 1 WHERE code = ? AND tenant_id = ?",
                (code, tid),
            )
        return True

    # ── Sync Events ──

    async def add_sync_event(
        self,
        space_id: str,
        event_type: str,
        event_data: Dict[str, Any],
        lamport_ts: int,
        node_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        tid = resolve_tenant_id(tenant_id)
        async with self.write_tx(tid) as h:
            res = await h.exec(
                "INSERT INTO sync_events (space_id, event_type, event_data, lamport_ts, node_id, "
                "created_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    space_id,
                    event_type,
                    json.dumps(event_data, ensure_ascii=False),
                    lamport_ts,
                    node_id,
                    datetime.now().isoformat(),
                    tid,
                ),
            )
            rowid = res.lastrowid
        return rowid

    # ── Row Converters ──

    @staticmethod
    def _row_to_space(row: aiosqlite.Row) -> Space:
        config_data = {}
        raw_config = row["config"]
        if raw_config:
            try:
                config_data = json.loads(raw_config)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Space config parse error for id={row['id']}")
        status_val = row["status"]
        if isinstance(status_val, str):
            try:
                status_val = SpaceStatus(status_val)
            except ValueError:
                pass
        return Space(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            owner_id=row["owner_id"],
            status=status_val,
            kb_bind_mode=row["kb_bind_mode"],
            kb_id=row["kb_id"],
            collab_mode=row["collab_mode"],
            config=SpaceConfig.from_dict(config_data),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row["tenant_id"],
        )

    @staticmethod
    def _row_to_member(row: aiosqlite.Row) -> SpaceMember:
        role_val = row["role"]
        if isinstance(role_val, str):
            try:
                role_val = SpaceRole(role_val)
            except ValueError:
                pass
        return SpaceMember(
            space_id=row["space_id"],
            user_id=row["user_id"],
            role=role_val,
            display_name=row["display_name"],
            joined_at=row["joined_at"],
            last_active=row["last_active"],
            tenant_id=row["tenant_id"],
        )

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> SpaceMessage:
        attachments = []
        raw_attachments = row["attachments"]
        if raw_attachments:
            try:
                attachments = json.loads(raw_attachments)
            except (json.JSONDecodeError, TypeError):
                pass
        metadata = {}
        raw_metadata = row["metadata"]
        if raw_metadata:
            try:
                metadata = json.loads(raw_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        return SpaceMessage(
            id=row["id"],
            space_id=row["space_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            role=row["role"],
            content=row["content"],
            content_type=row["content_type"],
            attachments=attachments,
            parent_msg_id=row["parent_msg_id"],
            thread_id=row["thread_id"],
            metadata=metadata,
            created_at=row["created_at"],
            tenant_id=row["tenant_id"],
        )

    @staticmethod
    def _row_to_snapshot(row: aiosqlite.Row) -> SpaceSnapshot:
        snapshot_data = {}
        raw = row["snapshot_data"]
        if raw:
            try:
                snapshot_data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return SpaceSnapshot(
            id=row["id"],
            space_id=row["space_id"],
            name=row["name"],
            messages_count=row["messages_count"],
            agents_count=row["agents_count"],
            files_count=row["files_count"],
            workflows_count=row["workflows_count"],
            artifacts_count=row["artifacts_count"],
            snapshot_data=snapshot_data,
            created_by=row["created_by"],
            created_at=row["created_at"],
            tenant_id=row["tenant_id"],
        )
