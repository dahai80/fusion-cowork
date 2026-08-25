"""迁移版本管理测试 (v0.4.0 Stage 3).

sqlite 路径始终跑 (aiosqlite 无需 asyncpg); MigrationRunner 幂等 + 版本递增 + 失败回滚。
"""

import aiosqlite
import pytest

from fusion_cowork.db.migrations import MIGRATIONS, Migration, MigrationRunner

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sqlite_runner(tmp_path):
    db_path = str(tmp_path / "mig.db")
    db = await aiosqlite.connect(db_path)
    # 建空表供 ADD COLUMN 迁移用 (模拟旧库 schema)
    for t in [
        "spaces",
        "space_members",
        "space_messages",
        "space_comments",
        "space_agents",
        "space_snapshots",
        "space_invite_links",
        "space_workflows",
        "space_artifacts",
        "sync_events",
    ]:
        await db.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY)")
    await db.commit()

    async def _exec(sql, params=(), read=False):
        cursor = await db.execute(sql, params or ())
        if read:
            return await cursor.fetchall()
        await db.commit()
        return None

    runner = MigrationRunner("sqlite", executor=_exec, error_class=aiosqlite.OperationalError)
    yield runner, db
    await db.close()


async def test_run_pending_applies_all(sqlite_runner):
    runner, db = sqlite_runner
    applied = await runner.run_pending()
    assert sorted(applied) == [1, 2]


async def test_run_pending_idempotent(sqlite_runner):
    runner, db = sqlite_runner
    first = await runner.run_pending()
    second = await runner.run_pending()
    assert first == [1, 2]
    assert second == []


async def test_version_recorded(sqlite_runner):
    runner, db = sqlite_runner
    await runner.run_pending()
    rows = await db.execute("SELECT version FROM schema_migrations ORDER BY version")
    versions = [r[0] for r in await rows.fetchall()]
    assert versions == [1, 2]


async def test_only_pending_applied(sqlite_runner):
    runner, db = sqlite_runner
    # 预置 v1 已应用
    await runner._ensure_migration_table()
    await runner._record(MIGRATIONS[0])
    applied = await runner.run_pending()
    assert applied == [2]


async def test_tenant_id_columns_added(sqlite_runner):
    runner, db = sqlite_runner
    await runner.run_pending()
    cols = await db.execute("PRAGMA table_info(spaces)")
    names = {r[1] for r in await cols.fetchall()}
    assert "tenant_id" in names


async def test_custom_migrations_set(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "c.db"))
    await db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    await db.commit()

    async def _exec(sql, params=(), read=False):
        cursor = await db.execute(sql, params or ())
        if read:
            return await cursor.fetchall()
        await db.commit()
        return None

    custom = [Migration(version=1, name="add_x", up_sql=["ALTER TABLE t ADD COLUMN x TEXT"])]
    runner = MigrationRunner("sqlite", executor=_exec, error_class=aiosqlite.OperationalError)
    applied = await runner.run_pending(custom)
    assert applied == [1]
    await db.close()
