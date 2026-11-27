"""Postgres row-level security policies (v0.4.0 Stage 3).

Defense-in-depth: when SQL misses the WHERE tenant_id=? guard, RLS still blocks cross-tenant reads/writes.
Postgres backend only; sqlite has no RLS and relies on query guards (added to all tables in Stage 1).

Usage:
- after initialize() creates tables, call apply_rls(conn) to enable RLS on the tables + build the tenant_isolation policy
- before each transaction, call set_tenant_context(conn, tenant_id) to set the app.tenant_id session variable
- policy: USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (same) — validated on writes too
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)

# The 14 tables that gained tenant_id in Stage 1 (incl. sidebar_modules + space_notifications)
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
    # FORCE: table owners/superusers are also bound by RLS (defense-in-depth — prevents owner bypass)
    return f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"


def create_policy_sql(table: str) -> str:
    return (
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}; "
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = current_setting('app.tenant_id', true)) "
        f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


async def apply_rls(conn: Any) -> None:
    """Enable RLS on all RLS_TABLES + build the tenant_isolation policy. postgres only.

    Defense-in-depth: when SQL misses the WHERE tenant_id=? guard, RLS blocks cross-tenant reads/writes.
    Idempotent: DROP POLICY IF EXISTS + ENABLE on an already-enabled table are no-ops.
    """
    for table in RLS_TABLES:
        await conn.execute(enable_rls_sql(table))
        await conn.execute(force_rls_sql(table))
        await conn.execute(create_policy_sql(table))
    logger.info(
        f"RLS enabled on {len(RLS_TABLES)} tables (ENABLE+FORCE, prevents owner bypass) [defense-in-depth tenant isolation]"
    )


async def set_tenant_context(conn: Any, tenant_id: str) -> None:
    """SET LOCAL app.tenant_id = <tenant>. Call before each transaction.

    RLS policies read current_setting('app.tenant_id'). Must be set inside a transaction (SET LOCAL),
    automatically cleared at transaction end, isolating connection reuse.
    """
    safe = tenant_id.replace("'", "''")
    await conn.execute(f"SET LOCAL app.tenant_id = '{safe}'")
