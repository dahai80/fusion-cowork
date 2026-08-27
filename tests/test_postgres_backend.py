"""Postgres 后端集成测试 (v0.4.0 Stage 3).

门控: pytest.importorskip("asyncpg") + env FUSION_PG_DSN。无则全 skip (不阻断 CI)。
本地: docker run -e POSTGRES_PASSWORD=x -e POSTGRES_DB=fusion -p 5432:5432 postgres:16
      FUSION_PG_DSN=postgresql://postgres:x@localhost:5432/fusion python -m pytest tests/test_postgres_backend.py -v
"""

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]

PG_DSN = os.environ.get("FUSION_PG_DSN")

if not PG_DSN:
    pytest.skip("FUSION_PG_DSN 未设 — 跳过 Postgres 集成测试", allow_module_level=True)


@pytest.fixture(scope="module")
def event_loop():
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    # 清干净测试库
    for t in [
        "schema_migrations",
        "sidebar_modules",
        "space_notifications",
        "sync_events",
        "space_artifacts",
        "space_workflows",
        "space_invite_links",
        "space_snapshots",
        "space_agents",
        "space_comments",
        "space_messages",
        "space_members",
        "spaces",
    ]:
        await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    yield conn
    await conn.close()


async def test_spacestore_postgres_init(pg_conn, tmp_path):
    from fusion_cowork.space.store import SpaceStore

    store = SpaceStore(data_dir=str(tmp_path), dsn=PG_DSN)
    await store.initialize()
    assert store.backend == "postgres"
    # 12 数据表 + sidebar_modules + space_notifications + schema_migrations = 15 (>=13 容差)
    row = await pg_conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    assert row >= 13
    await store.close()


async def test_placeholder_normalization():
    from fusion_cowork.db.placeholders import normalize_placeholders

    assert normalize_placeholders("SELECT * FROM t WHERE id = ? AND x = ?") == (
        "SELECT * FROM t WHERE id = $1 AND x = $2"
    )
    assert normalize_placeholders("SELECT 1") == "SELECT 1"


async def test_rls_blocks_cross_tenant(pg_conn, tmp_path):
    from fusion_cowork.db.migrations import MigrationRunner
    from fusion_cowork.db.placeholders import normalize_placeholders
    from fusion_cowork.db.rls import apply_rls, set_tenant_context
    from fusion_cowork.space.store import _pg_schema_sql

    await pg_conn.execute(_pg_schema_sql())

    async def _exec(sql, params=(), read=False):
        pgsql = normalize_placeholders(sql)
        if read:
            return await pg_conn.fetch(pgsql, *params) if params else await pg_conn.fetch(pgsql)
        await pg_conn.execute(pgsql, *params) if params else await pg_conn.execute(pgsql)

    await MigrationRunner("postgres", executor=_exec, error_class=Exception).run_pending()
    # 插两条不同租户数据 (spaces: created_at/updated_at TEXT NOT NULL 无默认, 须显式给)
    await pg_conn.execute(
        "INSERT INTO spaces (id, name, owner_id, created_at, updated_at, tenant_id) "
        "VALUES ($1, 'a', 'u1', '2026-01-01', '2026-01-01', 'tenantA')",
        "sp_a_" + uuid.uuid4().hex[:6],
    )
    await pg_conn.execute(
        "INSERT INTO spaces (id, name, owner_id, created_at, updated_at, tenant_id) "
        "VALUES ($1, 'b', 'u2', '2026-01-01', '2026-01-01', 'tenantB')",
        "sp_b_" + uuid.uuid4().hex[:6],
    )
    # RLS 建表主(超级用户)仍绕过 RLS; 须用非超级用户角色验证。
    # provision 非超级角色 + 授权 (postgres superuser 建角色)
    # 先清前次残留角色依赖 (DROP OWNED 再 DROP ROLE)
    try:
        await pg_conn.execute("DROP OWNED BY rls_test")
    except Exception:
        pass
    await pg_conn.execute("DROP ROLE IF EXISTS rls_test")
    await pg_conn.execute("CREATE ROLE rls_test NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'rls'")
    await pg_conn.execute("GRANT USAGE ON SCHEMA public TO rls_test")
    await pg_conn.execute("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO rls_test")
    await apply_rls(pg_conn)
    # 以 rls_test 角色另连验证 RLS 生效 — 通用 DSN 凭证替换 (勿硬编码 superuser 名)
    # CI/本地 DSN 用户名各异 (postgres / fusion / ...), 从 DSN 解析建表主用户名
    from urllib.parse import urlparse

    parsed = urlparse(PG_DSN)
    su_user = parsed.username or "postgres"
    su_host = parsed.hostname or "localhost"
    su_port = parsed.port or 5432
    su_db = parsed.path.lstrip("/") or "postgres"
    role_dsn = f"postgresql://rls_test:rls@{su_host}:{su_port}/{su_db}"
    rls_conn = await asyncpg.connect(role_dsn)
    try:
        # tenantA 上下文应只见 1 条 (RLS 挡 tenantB)
        await rls_conn.execute("BEGIN")
        await set_tenant_context(rls_conn, "tenantA")
        count = await rls_conn.fetchval("SELECT count(*) FROM spaces")
        await rls_conn.execute("COMMIT")
        assert count == 1
    finally:
        await rls_conn.close()
        # 清角色依赖: REASSIGN + DROP OWNED + REVOKE 再 DROP ROLE
        # REASSIGN 目标用解析出的建表主 (非硬编码 postgres, 否则角色不存在报错)
        await pg_conn.execute(f"REASSIGN OWNED BY rls_test TO {su_user}")
        await pg_conn.execute("DROP OWNED BY rls_test")
        await pg_conn.execute("DROP ROLE IF EXISTS rls_test")


async def test_store_crud_postgres(tmp_path):
    from fusion_cowork.space.models import Space
    from fusion_cowork.space.store import SpaceStore

    store = SpaceStore(data_dir=str(tmp_path), dsn=PG_DSN)
    await store.initialize()
    tid = "t_" + uuid.uuid4().hex[:6]
    sp = Space(name="pg-test", owner_id="u1")
    created = await store.create_space(sp, tenant_id=tid)
    got = await store.get_space(created.id, tenant_id=tid)
    assert got is not None
    assert got.name == "pg-test"
    # 跨租户不可见
    other = await store.get_space(created.id, tenant_id="other_tenant")
    assert other is None
    await store.close()


async def test_sessionstore_postgres(tmp_path):
    from fusion_cowork.engine.session import Session, SessionStore

    store = SessionStore(dsn=PG_DSN)
    assert store._backend == "postgres"
    tid = "st_" + uuid.uuid4().hex[:6]
    sess = Session(workflow_id="wf1", workflow_name="wfname", tenant_id=tid)
    store.save(sess)
    got = store.get(sess.id, tenant_id=tid)
    assert got is not None
    assert got.workflow_id == "wf1"
    # 跨租户不可见
    assert store.get(sess.id, tenant_id="other") is None
    store.close()
