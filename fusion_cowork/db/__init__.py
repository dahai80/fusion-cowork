"""数据库后端模块 (v0.4.0 Stage 3).

- migrations: 版本化迁移 (schema_migrations 表 + MigrationRunner)
- rls: Postgres 行级安全策略 (纵深防御租户隔离)
- backup: pg_dump 备份/恢复
- placeholders: ? → $1/$2 占位符归一化 (跨后端 SQL 复用)

单 SpaceStore 类按 dsn 选后端: 传 dsn 或 env FUSION_PG_DSN → postgres (asyncpg);
只传 data_dir → sqlite (aiosqlite)。946 测试零改动 (仍传 data_dir)。
"""

from fusion_cowork.db.backup import BackupManager
from fusion_cowork.db.migrations import MIGRATIONS, Migration, MigrationRunner
from fusion_cowork.db.placeholders import normalize_placeholders
from fusion_cowork.db.rls import apply_rls, set_tenant_context

__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationRunner",
    "apply_rls",
    "set_tenant_context",
    "BackupManager",
    "normalize_placeholders",
]
