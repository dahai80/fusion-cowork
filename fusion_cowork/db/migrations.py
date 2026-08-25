"""版本化数据库迁移 (v0.4.0 Stage 3).

替代 store.py 无版本管理盲 ALTER try/except:
- schema_migrations 表跟踪已应用版本
- MigrationRunner: 读已应用版本 → 按序 apply pending → 单事务记版本 (失败回滚)
- 幂等: 重复 run 已应用版本无副作用 (跳过)

v1 = 现 Stage 1 tenant_id 列迁移 (对旧库 ADD COLUMN; 新库 _SCHEMA_SQL 已含, idempotent)
v2 = 现 _MIGRATION_SQL (artifact 列 + sidebar_modules + space_notifications)
后续版本追加于此。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class Migration:
    """单条迁移: 版本号 + 名称 + 上行 SQL 语句列表 + 适用后端。"""

    version: int
    name: str
    up_sql: List[str] = field(default_factory=list)
    backend: str = "both"  # both | sqlite | postgres


# ── 迁移序列 (按 version 升序) ──
# v1: tenant_id 列迁移 — 对 Stage 1 之前的旧库补 ADD COLUMN (新库 _SCHEMA_SQL 已含, idempotent)
#     每条 ADD COLUMN 包 IF NOT EXISTS — Postgres 9.6+ / SQLite 不支持, 走 runner 的 OperationalError 吞错。
# v2: artifact 列 + sidebar_modules + space_notifications (原 store._MIGRATION_SQL)

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
]


_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT ''
)
"""


class MigrationRunner:
    """迁移执行器 — 跨 sqlite/postgres 后端。

    executor: 异步执行回调 async (sql, params?) -> rows (读) 或 None (写)。
        sqlite: 直接 aiosqlite conn (含 commit); postgres: asyncpg conn (execute/fetch)。
    error_class: 后端列已存在错误类型 (sqlite OperationalError / postgres asyncpg.DuplicateColumnError),
        runner 吞此错做 idempotent ADD COLUMN。
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
        """按 version 升序 apply 未应用的迁移; 返回本次应用的版本号列表。

        幂等: 已应用版本跳过。ADD COLUMN 列已存在 → 吞 error_class 继续 (idempotent)。
        CREATE TABLE/INDEX IF NOT EXISTS 自身幂等。
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
                    logger.debug(f"迁移 v{m.version} SQL 幂等跳过 (已存在): {sql[:60]}")
            await self._record(m)
            applied_now.append(m.version)
            logger.info(f"迁移 v{m.version} ({m.name}) 已应用 [backend={self.backend}]")
        return applied_now
