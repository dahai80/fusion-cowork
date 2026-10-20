"""#122: 旧库 (pre-v0.4, 无 tenant_id 列) 上 SpaceStore 初始化不再崩溃。

Contract:
- 建旧库表 (spaces/space_messages/space_members 无 tenant_id) + 种子行;
- SpaceStore.initialize() 不抛 OperationalError (v1 ADD COLUMN 先于 tenant 索引);
- 初始化后 tenant_id 列已补 + 4 条 tenant 索引存在;
- 新空间可在迁移后创建。
"""

from __future__ import annotations

import pytest

LEGACY_TABLES = """
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
    updated_at TEXT NOT NULL
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
    created_at TEXT NOT NULL
);
"""


def _seed_legacy_db(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_TABLES)
    conn.execute(
        "INSERT INTO spaces (id, name, created_at, updated_at) VALUES ('sp-old', 'legacy-space', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
async def legacy_db(tmp_path):
    db_path = tmp_path / "spaces.db"
    _seed_legacy_db(str(db_path))
    return tmp_path, db_path


class TestLegacyDbInit:
    async def test_initialize_survives_legacy_db(self, legacy_db):
        from fusion_cowork.space.store import SpaceStore

        tmp_path, db_path = legacy_db
        store = SpaceStore(data_dir=str(tmp_path))
        try:
            await store.initialize()
            spaces = await store.list_spaces()
            assert isinstance(spaces, list)
        finally:
            await store.close()

    async def test_tenant_indexes_created_after_migration(self, legacy_db):
        import sqlite3

        from fusion_cowork.space.store import SpaceStore

        tmp_path, db_path = legacy_db
        store = SpaceStore(data_dir=str(tmp_path))
        try:
            await store.initialize()
            conn = sqlite3.connect(str(db_path))
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(spaces)").fetchall()]
                assert "tenant_id" in cols
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%tenant'"
                    ).fetchall()
                }
                assert {
                    "idx_spaces_tenant",
                    "idx_space_members_tenant",
                    "idx_space_messages_tenant",
                    "idx_audit_log_tenant",
                } <= names
            finally:
                conn.close()
        finally:
            await store.close()

    async def test_new_space_on_migrated_db(self, legacy_db):
        from fusion_cowork.space.store import SpaceStore

        tmp_path, _ = legacy_db
        store = SpaceStore(data_dir=str(tmp_path))
        try:
            await store.initialize()
            from fusion_cowork.space.models import Space

            space = await store.create_space(Space(name="migrated-space", owner_id="u1"))
            assert space.id
        finally:
            await store.close()
