"""Postgres 行级安全策略 (v0.4.0 Stage 3).

纵深防御: SQL 漏 WHERE tenant_id=? 守卫时, RLS 仍挡跨租户读写。
仅 postgres backend; sqlite 无 RLS, 依赖查询守卫 (Stage 1 已全表加)。

用法:
- initialize() 建表后调 apply_rls(conn) 启用 14 表 RLS + 建 tenant_isolation 策略
- 每事务前调 set_tenant_context(conn, tenant_id) 设 app.tenant_id 会话变量
- 策略: USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (同) — 写时也校验
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)

# Stage 1 加了 tenant_id 的 14 表 (含 sidebar_modules + space_notifications)
RLS_TABLES: List[str] = [
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
    "sidebar_modules",
    "space_notifications",
]


def enable_rls_sql(table: str) -> str:
    return f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"


def force_rls_sql(table: str) -> str:
    # FORCE: 表主/超级用户也受 RLS 约束 (纵深防御 — 防 owner 绕过)
    return f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"


def create_policy_sql(table: str) -> str:
    return (
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}; "
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = current_setting('app.tenant_id', true)) "
        f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


async def apply_rls(conn: Any) -> None:
    """对 RLS_TABLES 全表启用 RLS + 建 tenant_isolation 策略。postgres only。

    纵深防御: SQL 漏 WHERE tenant_id=? 守卫时, RLS 挡跨租户读写。
    幂等: DROP POLICY IF EXISTS + ENABLE 已启用是 no-op。
    """
    for table in RLS_TABLES:
        await conn.execute(enable_rls_sql(table))
        await conn.execute(force_rls_sql(table))
        await conn.execute(create_policy_sql(table))
    logger.info(f"RLS 已启用 {len(RLS_TABLES)} 表 (ENABLE+FORCE, 防 owner 绕过) [纵深防御租户隔离]")


async def set_tenant_context(conn: Any, tenant_id: str) -> None:
    """SET LOCAL app.tenant_id = <tenant>。每事务前调。

    RLS 策略读 current_setting('app.tenant_id')。必须事务内设 (SET LOCAL),
    事务结束自动清除, 隔离连接复用。
    """
    safe = tenant_id.replace("'", "''")
    await conn.execute(f"SET LOCAL app.tenant_id = '{safe}'")
