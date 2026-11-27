"""Versioned database migrations (v0.4.0 Stage 3).

Replaces store.py's unversioned blind ALTER try/except:
- schema_migrations table tracks applied versions
- MigrationRunner: reads applied versions -> applies pending in order -> records version in a single transaction (rollback on failure)
- Idempotent: re-running already-applied versions has no side effects (skipped)

v1 = current Stage 1 tenant_id column migration (ADD COLUMN for legacy tables; new-table _SCHEMA_SQL already includes it, idempotent)
v2 = current _MIGRATION_SQL (artifact columns + sidebar_modules + space_notifications)
Later versions are appended here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class Migration:
    """Single migration: version + name + list of up SQL statements + target backend."""

    version: int
    name: str
    up_sql: List[str] = field(default_factory=list)
    backend: str = "both"  # both | sqlite | postgres


# ── Migration sequence (ascending by version) ──
# v1: tenant_id column migration — ADD COLUMN for legacy pre-Stage-1 tables (new-table _SCHEMA_SQL already includes it, idempotent)
#     Each ADD COLUMN would need IF NOT EXISTS — unsupported on Postgres 9.6+ / SQLite, handled via the runner's OperationalError swallow.
# v2: artifact columns + sidebar_modules + space_notifications (formerly store._MIGRATION_SQL)

_TENANT_ADD_COLUMN = [
    ("spaces", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_members", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_messages", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_comments", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_agents", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_snapshots", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_invite_links", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_workflows", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("space_artifacts", "tenant_id TEXT NOT NULL DEFAULT ''"),
    ("sync_events", "tenant_id TEXT NOT NULL DEFAULT ''"),
]

MIGRATIONS: List[Migration] = [
    Migration(
        version=1,
        name="tenant_id_columns",
        up_sql=[f"ALTER TABLE {t} ADD COLUMN {col}" for t, col in _TENANT_ADD_COLUMN],
        backend="both",
    ),
    Migration(
        version=2,
        name="artifact_cols_sidebar_notifications",
        up_sql=[
            "ALTER TABLE space_artifacts ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE space_artifacts ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE space_artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE space_artifacts ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
            (
                "CREATE TABLE IF NOT EXISTS sidebar_modules ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', "
                "route_path TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, "
                "metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "tenant_id TEXT NOT NULL DEFAULT '')"
            ),
            (
                "CREATE TABLE IF NOT EXISTS space_notifications ("
                "id TEXT PRIMARY KEY, space_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                "notification_type TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
                "content TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}', "
                "read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                "tenant_id TEXT NOT NULL DEFAULT '')"
            ),
            "CREATE INDEX IF NOT EXISTS idx_sidebar_modules_tenant ON sidebar_modules(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_space_notifications_tenant ON space_notifications(tenant_id)",
        ],
        backend="both",
    ),
    # v3: legacy tables (space_notifications / sidebar_modules created before v0.4.0) lack the tenant_id column —
    #     v2's CREATE TABLE IF NOT EXISTS is a no-op on existing tables so the column would never be added, and queries
    #     fail directly with `no such column: tenant_id` 500 (issue #253). Repeated ADD COLUMN is made idempotent by the runner's error swallow.
    Migration(
        version=3,
        name="legacy_tenant_columns",
        up_sql=[
            "ALTER TABLE space_notifications ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sidebar_modules ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''",
        ],
        backend="both",
    ),
]


_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT ''
)
"""


class MigrationRunner:
    """Migration runner — across sqlite/postgres backends.

    executor: async callback async (sql, params?) -> rows (read) or None (write).
        sqlite: raw aiosqlite conn (with commit); postgres: asyncpg conn (execute/fetch).
    error_class: backend column-already-exists error type (sqlite OperationalError / postgres asyncpg.DuplicateColumnError);
        the runner swallows this error to make ADD COLUMN idempotent.
    """

    def __init__(self, backend: str, executor: Any, error_class: Any = Exception, now_fn=None):
        self.backend = backend
        self._exec = executor
        self._error_class = error_class
        import datetime

        self._now_fn = now_fn or (lambda: datetime.datetime.now(datetime.UTC).isoformat())

    async def _ensure_migration_table(self) -> None:
        await self._exec(_MIGRATION_TABLE_SQL.strip())

    async def _applied_versions(self) -> Set[int]:
        rows = await self._exec("SELECT version FROM schema_migrations", (), read=True)
        return {int(r[0]) for r in (rows or [])}

    async def _record(self, m: Migration) -> None:
        await self._exec(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (m.version, m.name, self._now_fn()),
        )

    async def run_pending(self, migrations: Optional[List[Migration]] = None) -> List[int]:
        """Apply unapplied migrations in ascending version order; return the versions applied this run.

        Idempotent: already-applied versions are skipped. ADD COLUMN with an existing column -> swallow error_class and continue (idempotent).
        CREATE TABLE/INDEX IF NOT EXISTS is itself idempotent.
        """
        await self._ensure_migration_table()
        applied = await self._applied_versions()
        pending = sorted(
            [m for m in (migrations or MIGRATIONS) if m.version not in applied and m.backend in ("both", self.backend)],
            key=lambda x: x.version,
        )
        applied_now: List[int] = []
        for m in pending:
            for sql in m.up_sql:
                try:
                    await self._exec(sql)
                except self._error_class:
                    logger.debug(f"migration v{m.version} SQL idempotent skip (already exists): {sql[:60]}")
            await self._record(m)
            applied_now.append(m.version)
            logger.info(f"migration v{m.version} ({m.name}) applied [backend={self.backend}]")
        return applied_now
