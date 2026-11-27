"""Database backend module (v0.4.0 Stage 3).

- migrations: versioned migrations (schema_migrations table + MigrationRunner)
- rls: Postgres row-level security policies (defense-in-depth tenant isolation)
- backup: pg_dump backup/restore
- placeholders: ? → $1/$2 placeholder normalization (cross-backend SQL reuse)

A single SpaceStore class selects the backend by dsn: pass dsn or env DATABASE_URL (legacy name FUSION_PG_DSN, issue #221) → postgres (asyncpg);
pass data_dir only → sqlite (aiosqlite). 946 tests unchanged (still pass data_dir).
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
