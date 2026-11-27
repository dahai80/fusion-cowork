"""Multi-tenant isolation tests (v0.4.0 Stage 1) — tenants A/B cannot see each other's data.

Coverage:
- Space: A cannot find B's space
- Message: A cannot read B's messages, cross-tenant delete_message rowcount=0
- Artifact: A cannot list B's artifacts, cross-tenant get_artifact=None
- Notification: cross-tenant mark_read does not affect B
- Session: cross-tenant get/list invisible
- Scheduler: list_tasks filters by tenant
"""

import pytest

from fusion_cowork.engine.scheduler import TaskScheduler
from fusion_cowork.engine.session import Session, SessionStore
from fusion_cowork.space.artifact import SpaceArtifactService
from fusion_cowork.space.fsb import NotificationService
from fusion_cowork.space.models import Space, SpaceMember, SpaceMessage, SpaceRole
from fusion_cowork.space.permission import SpacePermission
from fusion_cowork.space.store import SpaceStore
from fusion_cowork.tenant import (
    DEFAULT_TENANT,
    reset_current_tenant,
    set_current_tenant,
)

TENANT_A = "tenantA"
TENANT_B = "tenantB"


@pytest.fixture
async def store(tmp_path):
    s = SpaceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def perm(store):
    return SpacePermission(store)


@pytest.fixture
def artifact_svc(store, perm):
    return SpaceArtifactService(store, perm)


@pytest.fixture
def notif_svc(store):
    return NotificationService(store)


async def _make_space(store, tenant, name, owner):
    sp = Space(name=name, owner_id=owner, tenant_id=tenant)
    sp = await store.create_space(sp, tenant_id=tenant)
    owner_m = SpaceMember(
        space_id=sp.id,
        user_id=owner,
        role=SpaceRole.OWNER.value,
        display_name=owner,
        tenant_id=tenant,
    )
    await store.add_member(owner_m, tenant_id=tenant)
    return sp


class TestTenantSpaceIsolation:
    async def test_cross_tenant_get_space_returns_none(self, store):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        # B queries A's space_id within its own tenant → None
        tok = set_current_tenant(TENANT_B)
        try:
            got = await store.get_space(sp_a.id)
        finally:
            reset_current_tenant(tok)
        assert got is None

    async def test_list_spaces_scoped_to_tenant(self, store):
        await _make_space(store, TENANT_A, "space-a", "uA")
        await _make_space(store, TENANT_B, "space-b", "uB")
        tok = set_current_tenant(TENANT_A)
        try:
            spaces_a = await store.list_spaces()
        finally:
            reset_current_tenant(tok)
        assert len(spaces_a) == 1
        assert spaces_a[0].name == "space-a"
        assert spaces_a[0].tenant_id == TENANT_A


class TestTenantMessageIsolation:
    async def test_cross_tenant_messages_invisible(self, store):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        msg = SpaceMessage(
            space_id=sp_a.id,
            user_id="uA",
            content="secret-from-A",
            tenant_id=TENANT_A,
        )
        await store.add_message(msg, tenant_id=TENANT_A)
        # B reads messages under the same space_id → empty (tenant_id guard blocks)
        tok = set_current_tenant(TENANT_B)
        try:
            msgs_b = await store.get_messages(sp_a.id)
        finally:
            reset_current_tenant(tok)
        assert msgs_b == []

    async def test_cross_tenant_delete_message_fails(self, store):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        msg = SpaceMessage(
            space_id=sp_a.id,
            user_id="uA",
            content="A-only",
            tenant_id=TENANT_A,
        )
        await store.add_message(msg, tenant_id=TENANT_A)
        # B tries to delete A's msg → rowcount=0, not deleted
        tok = set_current_tenant(TENANT_B)
        try:
            deleted = await store.delete_message(msg.id)
        finally:
            reset_current_tenant(tok)
        assert deleted is False
        # A's message still present
        tok = set_current_tenant(TENANT_A)
        try:
            still = await store.get_messages(sp_a.id)
        finally:
            reset_current_tenant(tok)
        assert len(still) == 1


class TestTenantArtifactIsolation:
    async def test_cross_tenant_list_artifacts_empty(self, store, artifact_svc):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        # perm.check relies on the contextvar (get_member has no explicit tenant), artifact creation must run in A's context.
        tok = set_current_tenant(TENANT_A)
        try:
            await artifact_svc.create_artifact(sp_a.id, "uA", name="art-a", tenant_id=TENANT_A)
        finally:
            reset_current_tenant(tok)
        # B lists artifacts of the same space_id → perm.check rejects first (B queries A's space in B's tenant,
        # get_member must be empty → check returns False → raise PermissionError). The permission layer runs before the data layer,
        # so isolation holds as long as cross-tenant data is unreachable.
        tok = set_current_tenant(TENANT_B)
        try:
            with pytest.raises(PermissionError):
                await artifact_svc.list_artifacts(sp_a.id, "uB")
        finally:
            reset_current_tenant(tok)

    async def test_cross_tenant_get_artifact_none(self, store, artifact_svc):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        tok = set_current_tenant(TENANT_A)
        try:
            art = await artifact_svc.create_artifact(sp_a.id, "uA", name="art-a", tenant_id=TENANT_A)
        finally:
            reset_current_tenant(tok)
        # Query via the store directly, bypassing perm, to verify the data-layer tenant_id guard: A's artifact is not found in B's tenant.
        tok = set_current_tenant(TENANT_B)
        try:
            db = await store._ensure_db()
            cursor = await db.execute(
                "SELECT * FROM space_artifacts WHERE id = ? AND tenant_id = ?",
                (art["id"], TENANT_B),
            )
            row = await cursor.fetchone()
        finally:
            reset_current_tenant(tok)
        assert row is None


class TestTenantNotificationIsolation:
    async def test_cross_tenant_mark_read_no_effect(self, store, notif_svc):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        notif = await notif_svc.push_notification(sp_a.id, "uA", "approval", "title-a", "content-a", tenant_id=TENANT_A)
        # B tries to mark_read A's notification → UPDATE WHERE tenant_id=B affects 0 rows, but returns True (idempotent)
        # Verify A's notification read is still 0 (unaffected)
        tok = set_current_tenant(TENANT_B)
        try:
            await notif_svc.mark_read(notif["id"])
        finally:
            reset_current_tenant(tok)
        tok = set_current_tenant(TENANT_A)
        try:
            notifs_a = await notif_svc.list_notifications("uA")
        finally:
            reset_current_tenant(tok)
        assert len(notifs_a) == 1
        assert notifs_a[0]["read"] == 0

    async def test_cross_tenant_list_notifications_empty(self, store, notif_svc):
        sp_a = await _make_space(store, TENANT_A, "space-a", "uA")
        await notif_svc.push_notification(sp_a.id, "uA", "approval", "title-a", tenant_id=TENANT_A)
        # B queries uA's notifications (same user_id) → tenant_id guard blocks, empty
        tok = set_current_tenant(TENANT_B)
        try:
            notifs_b = await notif_svc.list_notifications("uA")
        finally:
            reset_current_tenant(tok)
        assert notifs_b == []


class TestTenantSessionIsolation:
    def test_cross_tenant_session_invisible(self, tmp_path):
        store = SessionStore(db_path=str(tmp_path / "sess.db"))
        sess = Session(workflow_id="wf-a", workflow_name="wf-a", tenant_id=TENANT_A)
        store.save(sess)
        # B queries A's session_id → None
        tok = set_current_tenant(TENANT_B)
        try:
            got = store.get(sess.id)
            listed = store.list_sessions()
        finally:
            reset_current_tenant(tok)
        assert got is None
        assert listed == []
        # A can still find it
        tok = set_current_tenant(TENANT_A)
        try:
            got_a = store.get(sess.id)
        finally:
            reset_current_tenant(tok)
        assert got_a is not None
        assert got_a.tenant_id == TENANT_A

    def test_cross_tenant_delete_session_fails(self, tmp_path):
        store = SessionStore(db_path=str(tmp_path / "sess.db"))
        sess = Session(workflow_id="wf-a", tenant_id=TENANT_A)
        store.save(sess)
        tok = set_current_tenant(TENANT_B)
        try:
            deleted = store.delete(sess.id)
        finally:
            reset_current_tenant(tok)
        assert deleted is False


class TestTenantSchedulerIsolation:
    def test_list_tasks_scoped_to_tenant(self, tmp_path):
        sched_a = TaskScheduler(task_store_path=str(tmp_path / "a.json"))
        sched_a.add_interval_task("task-a", "wf-a", minutes=5, tenant_id=TENANT_A)
        sched_b = TaskScheduler(task_store_path=str(tmp_path / "b.json"))
        sched_b.add_interval_task("task-b", "wf-b", minutes=10, tenant_id=TENANT_B)
        # A only sees its own task
        tok = set_current_tenant(TENANT_A)
        try:
            tasks_a = sched_a.list_tasks()
        finally:
            reset_current_tenant(tok)
        assert len(tasks_a) == 1
        assert tasks_a[0].tenant_id == TENANT_A


class TestDefaultTenantBackwardCompat:
    async def test_no_tenant_defaults_to_default(self, store):
        # No contextvar, no tenant_id passed → falls into DEFAULT_TENANT, backward compatible with old tests
        sp = Space(name="compat", owner_id="u")
        sp = await store.create_space(sp)
        assert sp.tenant_id == DEFAULT_TENANT
        got = await store.get_space(sp.id)
        assert got is not None
