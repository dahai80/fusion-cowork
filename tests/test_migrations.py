"""Migration version-management tests (v0.4.0 Stage 3).

The sqlite path always runs (aiosqlite needs no asyncpg); MigrationRunner idempotency
+ version increments + failure rollback.
"""

import aiosqlite
import pytest

from fusion_cowork.db.migrations import MIGRATIONS, Migration, MigrationRunner

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sqlite_runner(tmp_path):
    db_path = str(tmp_path / "mig.db")
    db = await aiosqlite.connect(db_path)
    # Create empty tables for the ADD COLUMN migrations (simulating a legacy schema)
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
    # issue #253: simulate pre-v0.4.0 legacy tables — space_notifications / sidebar_modules without tenant_id
    await db.execute(
        "CREATE TABLE space_notifications (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)"
    )
    await db.execute("CREATE TABLE sidebar_modules (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '')")
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
    assert sorted(applied) == [1, 2, 3]


async def test_run_pending_idempotent(sqlite_runner):
    runner, db = sqlite_runner
    first = await runner.run_pending()
    second = await runner.run_pending()
    assert first == [1, 2, 3]
    assert second == []


async def test_version_recorded(sqlite_runner):
    runner, db = sqlite_runner
    await runner.run_pending()
    rows = await db.execute("SELECT version FROM schema_migrations ORDER BY version")
    versions = [r[0] for r in await rows.fetchall()]
    assert versions == [1, 2, 3]


async def test_only_pending_applied(sqlite_runner):
    runner, db = sqlite_runner
    # Preset v1 as already applied
    await runner._ensure_migration_table()
    await runner._record(MIGRATIONS[0])
    applied = await runner.run_pending()
    assert applied == [2, 3]


async def test_tenant_id_columns_added(sqlite_runner):
    runner, db = sqlite_runner
    await runner.run_pending()
    cols = await db.execute("PRAGMA table_info(spaces)")
    names = {r[1] for r in await cols.fetchall()}
    assert "tenant_id" in names


async def test_v3_legacy_tables_get_tenant_id(sqlite_runner):
    # issue #253: legacy space_notifications / sidebar_modules tables get tenant_id; notification queries no longer 500
    runner, db = sqlite_runner
    await runner.run_pending()
    for t in ("space_notifications", "sidebar_modules"):
        cols = await db.execute(f"PRAGMA table_info({t})")
        names = {r[1] for r in await cols.fetchall()}
        assert "tenant_id" in names, t
    rows = await db.execute("SELECT * FROM space_notifications WHERE tenant_id = ''")
    assert await rows.fetchall() == []


async def test_v3_skipped_when_already_applied(sqlite_runner):
    # Repeated init on a DB that already recorded v3 must not replay ADD COLUMN (runner swallows duplicate-column errors, idempotent)
    runner, db = sqlite_runner
    first = await runner.run_pending()
    second = await runner.run_pending()
    assert 3 in first
    assert second == []


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
