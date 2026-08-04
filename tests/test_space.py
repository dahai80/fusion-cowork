"""Fusion-Cowork 协作空间模块单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from fusion_cowork.space.chat import SpaceChatService
from fusion_cowork.space.knowledge import SpaceKBService
from fusion_cowork.space.member import SpaceMemberService
from fusion_cowork.space.models import (
    PeerInfo,
    Space,
    SpaceConfig,
    SpaceMember,
    SpaceMessage,
    SpaceRole,
    SpaceSnapshot,
    SpaceStatus,
)
from fusion_cowork.space.permission import SpacePermission
from fusion_cowork.space.service import SpaceService
from fusion_cowork.space.store import SpaceStore


def _make_space(name="test", owner_id="u1", **kw):
    return Space(
        id=kw.get("id", f"sp_{name}"),
        name=name,
        description=kw.get("description", ""),
        owner_id=owner_id,
        status=kw.get("status", SpaceStatus.ACTIVE),
        kb_bind_mode=kw.get("kb_bind_mode", "new_private"),
        kb_id=kw.get("kb_id"),
        collab_mode=kw.get("collab_mode", "local"),
        config=kw.get("config", SpaceConfig()),
        created_at=kw.get("created_at", datetime.now(UTC).isoformat()),
        updated_at=kw.get("updated_at", datetime.now(UTC).isoformat()),
    )


def _make_member(space_id, user_id, role=SpaceRole.MEMBER, display_name=""):
    now = datetime.now(UTC).isoformat()
    return SpaceMember(
        space_id=space_id, user_id=user_id, role=role,
        display_name=display_name or user_id,
        joined_at=now, last_active=now,
    )


@pytest.fixture
async def store(tmp_path):
    s = SpaceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def svc(store):
    return SpaceService(store)


@pytest.fixture
def perm(store):
    return SpacePermission(store)


@pytest.fixture
def member_svc(store, perm):
    return SpaceMemberService(store, perm)


# ── Models ──

class TestSpaceRole:
    def test_values(self):
        assert SpaceRole.OWNER.value == "owner"
        assert SpaceRole.ADMIN.value == "admin"
        assert SpaceRole.MEMBER.value == "member"
        assert SpaceRole.VIEWER.value == "viewer"


class TestSpaceStatus:
    def test_values(self):
        assert SpaceStatus.ACTIVE.value == "active"
        assert SpaceStatus.ARCHIVED.value == "archived"
        assert SpaceStatus.DELETED.value == "deleted"


class TestSpaceConfig:
    def test_defaults(self):
        cfg = SpaceConfig()
        assert cfg.enable_web_search is True
        assert cfg.max_members == 20
        assert cfg.stream_response is True
        assert cfg.default_model == ""

    def test_to_dict(self):
        cfg = SpaceConfig(max_members=10, enable_web_search=False)
        d = cfg.to_dict()
        assert d["max_members"] == 10
        assert d["enable_web_search"] is False

    def test_from_dict(self):
        d = {"max_members": 5, "enable_deep_research": False}
        cfg = SpaceConfig.from_dict(d)
        assert cfg.max_members == 5
        assert cfg.enable_deep_research is False

    def test_roundtrip(self):
        cfg = SpaceConfig(max_members=15, allow_member_agent=True, default_model="qwen")
        d = cfg.to_dict()
        cfg2 = SpaceConfig.from_dict(d)
        assert cfg2.max_members == 15
        assert cfg2.allow_member_agent is True
        assert cfg2.default_model == "qwen"


class TestSpace:
    def test_to_dict(self):
        cfg = SpaceConfig()
        sp = Space(id="sp_1", name="test", owner_id="u1", config=cfg)
        d = sp.to_dict()
        assert d["id"] == "sp_1"
        assert d["name"] == "test"
        assert isinstance(d["config"], dict)

    def test_from_dict(self):
        d = {
            "id": "sp_2", "name": "hello", "description": "desc",
            "owner_id": "u2", "status": "active", "kb_bind_mode": "new_private",
            "kb_id": None, "collab_mode": "local",
            "config": {"max_members": 20, "enable_web_search": True,
                       "enable_deep_research": True, "enable_computer_use": False,
                       "allow_member_upload": True, "allow_member_agent": True,
                       "allow_member_workflow": True, "auto_archive_days": 0,
                       "stream_response": True, "default_model": ""},
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        }
        sp = Space.from_dict(d)
        assert sp.id == "sp_2"
        assert sp.config.max_members == 20


class TestSpaceMember:
    def test_to_dict_from_dict(self):
        m = SpaceMember(space_id="sp_1", user_id="u1", role=SpaceRole.OWNER,
                        display_name="Alice")
        d = m.to_dict()
        m2 = SpaceMember.from_dict(d)
        assert m2.space_id == "sp_1"
        assert m2.role == SpaceRole.OWNER
        assert m2.display_name == "Alice"


class TestSpaceMessage:
    def test_to_dict_from_dict(self):
        msg = SpaceMessage(id="msg_1", space_id="sp_1", user_id="u1",
                           content="hello", content_type="text")
        d = msg.to_dict()
        m2 = SpaceMessage.from_dict(d)
        assert m2.id == "msg_1"
        assert m2.content == "hello"
        assert m2.attachments == []

    def test_with_attachments(self):
        msg = SpaceMessage(id="msg_2", space_id="sp_1", user_id="u1",
                           content="see file", attachments=[{"name": "a.pdf"}])
        d = msg.to_dict()
        m2 = SpaceMessage.from_dict(d)
        assert len(m2.attachments) == 1


class TestPeerInfo:
    def test_to_dict_from_dict(self):
        p = PeerInfo(user_id="u1", display_name="Bob", host="127.0.0.1",
                     port=9000, space_ids=["sp_1"])
        d = p.to_dict()
        p2 = PeerInfo.from_dict(d)
        assert p2.host == "127.0.0.1"
        assert p2.space_ids == ["sp_1"]


# ── Store ──

class TestSpaceStore:
    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, store):
        async with store._db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = [r[0] for r in await cur.fetchall()]
        assert "spaces" in tables
        assert "space_members" in tables
        assert "space_messages" in tables

    @pytest.mark.asyncio
    async def test_create_and_get_space(self, store):
        sp = _make_space("test", "u1")
        created = await store.create_space(sp)
        assert created.id.startswith("sp_")
        got = await store.get_space(created.id)
        assert got is not None
        assert got.name == "test"

    @pytest.mark.asyncio
    async def test_list_spaces(self, store):
        await store.create_space(_make_space("a", "u1"))
        await store.create_space(_make_space("b", "u2"))
        all_spaces = await store.list_spaces()
        assert len(all_spaces) == 2
        by_owner = await store.list_spaces(owner_id="u1")
        assert len(by_owner) == 1

    @pytest.mark.asyncio
    async def test_update_space(self, store):
        sp = await store.create_space(_make_space("old", "u1"))
        updated = await store.update_space(sp.id, name="new", description="desc")
        assert updated.name == "new"
        assert updated.description == "desc"

    @pytest.mark.asyncio
    async def test_delete_space_cascades(self, store):
        sp = await store.create_space(_make_space("del", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.delete_space(sp.id)
        assert await store.get_space(sp.id) is None
        members = await store.list_members(sp.id)
        assert len(members) == 0

    @pytest.mark.asyncio
    async def test_add_and_get_member(self, store):
        sp = await store.create_space(_make_space("m", "u1"))
        m = await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER, "Bob"))
        assert m.user_id == "u2"
        got = await store.get_member(sp.id, "u2")
        assert got is not None
        assert got.role == SpaceRole.MEMBER

    @pytest.mark.asyncio
    async def test_list_members(self, store):
        sp = await store.create_space(_make_space("ml", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        members = await store.list_members(sp.id)
        assert len(members) == 2

    @pytest.mark.asyncio
    async def test_update_member(self, store):
        sp = await store.create_space(_make_space("mu", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER, "Bob"))
        updated = await store.update_member(sp.id, "u2", role=SpaceRole.ADMIN)
        assert updated.role == SpaceRole.ADMIN

    @pytest.mark.asyncio
    async def test_remove_member(self, store):
        sp = await store.create_space(_make_space("mr", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER, "Bob"))
        removed = await store.remove_member(sp.id, "u2")
        assert removed is True
        assert await store.get_member(sp.id, "u2") is None

    @pytest.mark.asyncio
    async def test_count_members(self, store):
        sp = await store.create_space(_make_space("mc", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        count = await store.count_members(sp.id)
        assert count == 2

    @pytest.mark.asyncio
    async def test_add_and_get_message(self, store):
        sp = await store.create_space(_make_space("msg", "u1"))
        msg = SpaceMessage(id="msg_test1", space_id=sp.id, user_id="u1", content="hello")
        created = await store.add_message(msg)
        assert created.id.startswith("msg_")
        assert created.content == "hello"

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, store):
        sp = await store.create_space(_make_space("mp", "u1"))
        for i in range(5):
            m = SpaceMessage(id=f"msg_p{i}", space_id=sp.id, user_id="u1", content=f"msg{i}")
            await store.add_message(m)
        msgs = await store.get_messages(sp.id, limit=3)
        assert len(msgs) == 3

    @pytest.mark.asyncio
    async def test_delete_message(self, store):
        sp = await store.create_space(_make_space("md", "u1"))
        msg = SpaceMessage(id="msg_del1", space_id=sp.id, user_id="u1", content="del me")
        await store.add_message(msg)
        deleted = await store.delete_message("msg_del1")
        assert deleted is True
        count = await store.count_messages(sp.id)
        assert count == 0

    @pytest.mark.asyncio
    async def test_create_and_list_snapshots(self, store):
        sp = await store.create_space(_make_space("snap", "u1"))
        snap = SpaceSnapshot(
            id="snap_test1", space_id=sp.id, name="v1",
            messages_count=1, agents_count=0, files_count=0,
            workflows_count=0, artifacts_count=0, snapshot_data={"k": "v"},
            created_by="u1",
        )
        created = await store.create_snapshot(snap)
        assert created.id.startswith("snap_")
        snaps = await store.list_snapshots(sp.id)
        assert len(snaps) == 1

    @pytest.mark.asyncio
    async def test_create_and_use_invite(self, store):
        sp = await store.create_space(_make_space("inv", "u1"))
        code = await store.create_invite(
            code="inv_test1", space_id=sp.id,
            role=SpaceRole.MEMBER.value, created_by="u1", max_uses=2,
        )
        assert code.startswith("inv_")
        got = await store.get_invite(code)
        assert got is not None
        assert got["space_id"] == sp.id
        used = await store.use_invite(code)
        assert used is True
        got2 = await store.get_invite(code)
        assert got2["uses"] == 1

    @pytest.mark.asyncio
    async def test_add_sync_event(self, store):
        sp = await store.create_space(_make_space("sync", "u1"))
        row_id = await store.add_sync_event(
            space_id=sp.id, event_type="member_join",
            event_data={"user_id": "u2", "role": "member"},
            lamport_ts=1, node_id="node1",
        )
        assert row_id > 0


# ── Service ──

class TestSpaceService:
    @pytest.mark.asyncio
    async def test_create_adds_owner_as_member(self, svc, store):
        sp = await svc.create(name="test", owner_id="u1")
        member = await store.get_member(sp.id, "u1")
        assert member is not None
        assert member.role == SpaceRole.OWNER

    @pytest.mark.asyncio
    async def test_get(self, svc):
        sp = await svc.create(name="g", owner_id="u1")
        got = await svc.get(sp.id)
        assert got.name == "g"

    @pytest.mark.asyncio
    async def test_list_with_filter(self, svc):
        await svc.create(name="a", owner_id="u1")
        sp2 = await svc.create(name="b", owner_id="u2")
        await svc.archive(sp2.id)
        active = await svc.list(status="active")
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_update(self, svc):
        sp = await svc.create(name="old", owner_id="u1")
        updated = await svc.update(sp.id, name="new")
        assert updated.name == "new"

    @pytest.mark.asyncio
    async def test_archive_and_unarchive(self, svc):
        sp = await svc.create(name="arc", owner_id="u1")
        result = await svc.archive(sp.id)
        assert result is not None
        got = await svc.get(sp.id)
        assert got.status == SpaceStatus.ARCHIVED
        result2 = await svc.unarchive(sp.id)
        assert result2 is not None
        got2 = await svc.get(sp.id)
        assert got2.status == SpaceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_delete(self, svc, store):
        sp = await svc.create(name="del", owner_id="u1")
        await svc.delete(sp.id)
        assert await store.get_space(sp.id) is None

    @pytest.mark.asyncio
    async def test_get_or_create_creates(self, svc):
        sp = await svc.get_or_create(name="goc", owner_id="u1")
        assert sp.name == "goc"

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self, svc):
        sp1 = await svc.create(name="exist", owner_id="u1")
        sp2 = await svc.get_or_create(name="exist", owner_id="u1")
        assert sp2.id == sp1.id


# ── Permission ──

class TestSpacePermission:
    @pytest.mark.asyncio
    async def test_owner_has_all_permissions(self, perm, store):
        sp = await store.create_space(_make_space("p", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        for action in ["manage_space", "manage_members", "send_message",
                       "manage_agents", "run_workflow", "upload_file",
                       "delete_data", "manage_snapshots"]:
            assert await perm.check(sp.id, "u1", action) is True

    @pytest.mark.asyncio
    async def test_viewer_has_no_permissions(self, perm, store):
        sp = await store.create_space(_make_space("pv", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.VIEWER))
        for action in ["manage_space", "manage_members", "send_message",
                       "manage_agents", "run_workflow", "upload_file",
                       "delete_data", "manage_snapshots"]:
            assert await perm.check(sp.id, "u2", action) is False

    @pytest.mark.asyncio
    async def test_member_limited_permissions(self, perm, store):
        sp = await store.create_space(_make_space("pm", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        assert await perm.check(sp.id, "u2", "send_message") is True
        assert await perm.check(sp.id, "u2", "run_workflow") is True
        assert await perm.check(sp.id, "u2", "upload_file") is True
        assert await perm.check(sp.id, "u2", "manage_space") is False
        assert await perm.check(sp.id, "u2", "manage_members") is False

    @pytest.mark.asyncio
    async def test_non_member_denied(self, perm, store):
        sp = await store.create_space(_make_space("pn", "u1"))
        assert await perm.check(sp.id, "stranger", "send_message") is False

    @pytest.mark.asyncio
    async def test_get_role(self, perm, store):
        sp = await store.create_space(_make_space("pr", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        role = await perm.get_role(sp.id, "u1")
        assert role == SpaceRole.OWNER

    @pytest.mark.asyncio
    async def test_is_owner_or_admin(self, perm, store):
        sp = await store.create_space(_make_space("poa", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.ADMIN))
        await store.add_member(_make_member(sp.id, "u3", SpaceRole.MEMBER))
        assert await perm.is_owner_or_admin(sp.id, "u1") is True
        assert await perm.is_owner_or_admin(sp.id, "u2") is True
        assert await perm.is_owner_or_admin(sp.id, "u3") is False

    def test_get_permissions_for_role(self):
        owner_perms = SpacePermission.get_permissions_for_role(SpaceRole.OWNER)
        assert all(owner_perms.values())
        viewer_perms = SpacePermission.get_permissions_for_role(SpaceRole.VIEWER)
        assert not any(viewer_perms.values())

    def test_list_roles(self):
        roles = SpacePermission.list_roles()
        assert len(roles) == 4
        assert SpaceRole.OWNER.value in roles


# ── Member Service ──

class TestSpaceMemberService:
    @pytest.mark.asyncio
    async def test_invite_creates_code(self, member_svc, store):
        sp = await store.create_space(_make_space("mi", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        code = await member_svc.invite(sp.id, "u1", role=SpaceRole.MEMBER.value)
        assert code.startswith("inv_")

    @pytest.mark.asyncio
    async def test_invite_non_admin_denied(self, member_svc, store):
        sp = await store.create_space(_make_space("md", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        with pytest.raises(PermissionError):
            await member_svc.invite(sp.id, "u2", role=SpaceRole.MEMBER.value)

    @pytest.mark.asyncio
    async def test_join_via_invite(self, member_svc, store):
        sp = await store.create_space(_make_space("mj", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        code = await member_svc.invite(sp.id, "u1", role=SpaceRole.MEMBER.value)
        member = await member_svc.join(code, user_id="u2", display_name="Bob")
        assert member.space_id == sp.id
        assert member.role == SpaceRole.MEMBER

    @pytest.mark.asyncio
    async def test_join_expired_invite_fails(self, member_svc, store):
        sp = await store.create_space(_make_space("me", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        code = await member_svc.invite(sp.id, "u1", role=SpaceRole.MEMBER.value,
                                       expires_hours=-1)
        with pytest.raises(ValueError, match="过期"):
            await member_svc.join(code, user_id="u2")

    @pytest.mark.asyncio
    async def test_join_used_up_invite_fails(self, member_svc, store):
        sp = await store.create_space(_make_space("mu", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        code = await member_svc.invite(sp.id, "u1", role=SpaceRole.MEMBER.value, max_uses=1)
        await member_svc.join(code, user_id="u2")
        with pytest.raises(ValueError, match="已用完"):
            await member_svc.join(code, user_id="u3")

    @pytest.mark.asyncio
    async def test_add_direct(self, member_svc, store):
        sp = await store.create_space(_make_space("mad", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        m = await member_svc.add_direct(sp.id, "u2", display_name="Bob",
                                        operator_id="u1", role=SpaceRole.MEMBER)
        assert m.user_id == "u2"

    @pytest.mark.asyncio
    async def test_leave(self, member_svc, store):
        sp = await store.create_space(_make_space("ml", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        await member_svc.leave(sp.id, "u2")
        assert await store.get_member(sp.id, "u2") is None

    @pytest.mark.asyncio
    async def test_owner_cannot_leave(self, member_svc, store):
        sp = await store.create_space(_make_space("mol", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        with pytest.raises(ValueError, match="Owner"):
            await member_svc.leave(sp.id, "u1")

    @pytest.mark.asyncio
    async def test_update_role(self, member_svc, store):
        sp = await store.create_space(_make_space("mur", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        updated = await member_svc.update_role(sp.id, "u2", SpaceRole.ADMIN, operator_id="u1")
        assert updated.role == SpaceRole.ADMIN

    @pytest.mark.asyncio
    async def test_update_role_owner_transfer(self, member_svc, store):
        sp = await store.create_space(_make_space("mot", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        updated = await member_svc.update_role(sp.id, "u2", SpaceRole.OWNER, operator_id="u1")
        assert updated.role == SpaceRole.OWNER
        old_owner = await store.get_member(sp.id, "u1")
        assert old_owner.role == SpaceRole.ADMIN

    @pytest.mark.asyncio
    async def test_remove_member(self, member_svc, store):
        sp = await store.create_space(_make_space("mrm", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        removed = await member_svc.remove(sp.id, "u2", operator_id="u1")
        assert removed is True

    @pytest.mark.asyncio
    async def test_cannot_remove_owner(self, member_svc, store):
        sp = await store.create_space(_make_space("mro", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        with pytest.raises(ValueError, match="Owner"):
            await member_svc.remove(sp.id, "u1", operator_id="u1")

    @pytest.mark.asyncio
    async def test_list_members(self, member_svc, store):
        sp = await store.create_space(_make_space("mlm", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        members = await member_svc.list_members(sp.id)
        assert len(members) >= 1

    @pytest.mark.asyncio
    async def test_get_member(self, member_svc, store):
        sp = await store.create_space(_make_space("mgm", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        m = await member_svc.get_member(sp.id, "u1")
        assert m is not None
        assert m.role == SpaceRole.OWNER


# ── M7 Chat Service ──

class TestSpaceChatService:
    @pytest.fixture
    def mlx_mock(self):
        mock = AsyncMock()
        mock.list_models = AsyncMock(return_value=[{"id": "test-model"}])
        mock.stream_chat = AsyncMock()
        mock.stream_chat.return_value = _async_gen(["Hello", " world"])
        return mock

    @pytest.fixture
    def chat_svc(self, store, perm, mlx_mock):
        return SpaceChatService(store, mlx_mock, perm)

    @pytest.mark.asyncio
    async def test_send_message(self, chat_svc, store):
        sp = await store.create_space(_make_space("chat", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.MEMBER))
        msg = await chat_svc.send_message(sp.id, "u1", "hello")
        assert msg.content == "hello"
        assert msg.space_id == sp.id

    @pytest.mark.asyncio
    async def test_send_message_permission_denied(self, chat_svc, store):
        sp = await store.create_space(_make_space("chatp", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.VIEWER))
        with pytest.raises(PermissionError):
            await chat_svc.send_message(sp.id, "u2", "hello")

    @pytest.mark.asyncio
    async def test_get_context(self, chat_svc, store):
        sp = await store.create_space(_make_space("chatctx", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.MEMBER))
        await chat_svc.send_message(sp.id, "u1", "msg1")
        await chat_svc.send_message(sp.id, "u1", "msg2")
        ctx = await chat_svc.get_context(sp.id)
        assert len(ctx) >= 2

    @pytest.mark.asyncio
    async def test_list_messages(self, chat_svc, store):
        sp = await store.create_space(_make_space("chatlm", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.MEMBER))
        await chat_svc.send_message(sp.id, "u1", "hello")
        msgs = await chat_svc.list_messages(sp.id)
        assert len(msgs) >= 1

    @pytest.mark.asyncio
    async def test_build_messages(self, chat_svc):
        msgs = [
            SpaceMessage(role="user", content="hi"),
            SpaceMessage(role="assistant", content="hello"),
            SpaceMessage(role="system", content="info"),
        ]
        built = chat_svc._build_messages(msgs)
        assert len(built) == 3
        assert built[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_inject_rag(self, chat_svc):
        messages = [{"role": "system", "content": "You are helpful"}]
        rag = [{"content": "doc1"}, {"content": "doc2"}]
        result = chat_svc._inject_rag(messages, rag)
        assert len(result) == 2
        assert result[0]["content"] == "You are helpful"
        assert "doc1" in result[1]["content"]

    @pytest.mark.asyncio
    async def test_inject_rag_no_system(self, chat_svc):
        messages = [{"role": "user", "content": "hi"}]
        rag = [{"content": "ref"}]
        result = chat_svc._inject_rag(messages, rag)
        assert len(result) == 2
        assert result[0]["role"] == "system"


# ── M7 KB Service ──

class TestSpaceKBService:
    @pytest.fixture
    def kb_mock(self):
        mock = AsyncMock()
        mock.list_bases = AsyncMock(return_value=[])
        mock.create_kb = AsyncMock(return_value="kb_test123")
        mock.search = AsyncMock(return_value=[{"content": "result", "score": 0.9}])
        mock.query = AsyncMock(return_value="answer from KB")
        mock.upload_file = AsyncMock(return_value={"status": "ok"})
        mock.list_documents = AsyncMock(return_value=[{"name": "doc1.pdf"}])
        mock.health = AsyncMock(return_value=True)
        return mock

    @pytest.fixture
    def kb_svc(self, store, perm, kb_mock):
        return SpaceKBService(store, kb_mock, perm)

    @pytest.mark.asyncio
    async def test_bind_kb_creates_new(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kb1", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        kb_id = await kb_svc.bind_kb(sp.id, "u1")
        assert kb_id == "kb_test123"
        updated = await store.get_space(sp.id)
        assert updated.kb_id == "kb_test123"

    @pytest.mark.asyncio
    async def test_bind_kb_existing(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kb2", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        kb_mock.list_bases = AsyncMock(return_value=[{"id": "kb_exist"}])
        kb_id = await kb_svc.bind_kb(sp.id, "u1", kb_id="kb_exist")
        assert kb_id == "kb_exist"

    @pytest.mark.asyncio
    async def test_bind_kb_permission_denied(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbp", "u1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.MEMBER))
        with pytest.raises(PermissionError):
            await kb_svc.bind_kb(sp.id, "u2")

    @pytest.mark.asyncio
    async def test_bind_kb_no_kb_client(self, store, perm):
        svc = SpaceKBService(store, None, perm)
        sp = await store.create_space(_make_space("kbn", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        kb_id = await svc.bind_kb(sp.id, "u1")
        assert kb_id == f"kb_{sp.id}"
        updated = await store.get_space(sp.id)
        assert updated.kb_id == kb_id

    @pytest.mark.asyncio
    async def test_search(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kbs", "u1", kb_id="kb_1"))
        results = await kb_svc.search(sp.id, "test query")
        assert len(results) == 1
        assert results[0]["content"] == "result"

    @pytest.mark.asyncio
    async def test_search_no_kb_bound(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbsn", "u1"))
        results = await kb_svc.search(sp.id, "test")
        assert results == []

    @pytest.mark.asyncio
    async def test_query(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kbq", "u1", kb_id="kb_1"))
        answer = await kb_svc.query(sp.id, "what is X?")
        assert answer == "answer from KB"

    @pytest.mark.asyncio
    async def test_upload_document(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kbu", "u1", kb_id="kb_1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.MEMBER))
        result = await kb_svc.upload_document(sp.id, "u1", "/tmp/test.pdf")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_upload_document_permission_denied(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbup", "u1", kb_id="kb_1"))
        await store.add_member(_make_member(sp.id, "u2", SpaceRole.VIEWER))
        with pytest.raises(PermissionError):
            await kb_svc.upload_document(sp.id, "u2", "/tmp/test.pdf")

    @pytest.mark.asyncio
    async def test_upload_no_kb_bound(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbun", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.MEMBER))
        with pytest.raises(ValueError, match="尚未绑定知识库"):
            await kb_svc.upload_document(sp.id, "u1", "/tmp/test.pdf")

    @pytest.mark.asyncio
    async def test_list_documents(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kbld", "u1", kb_id="kb_1"))
        docs = await kb_svc.list_documents(sp.id)
        assert len(docs) == 1

    @pytest.mark.asyncio
    async def test_unbind_kb(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbum", "u1", kb_id="kb_1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        result = await kb_svc.unbind_kb(sp.id, "u1")
        assert result is True
        updated = await store.get_space(sp.id)
        assert updated.kb_id is None

    @pytest.mark.asyncio
    async def test_unbind_kb_not_bound(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbun2", "u1"))
        await store.add_member(_make_member(sp.id, "u1", SpaceRole.OWNER))
        result = await kb_svc.unbind_kb(sp.id, "u1")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_kb_status_bound(self, kb_svc, store, kb_mock):
        sp = await store.create_space(_make_space("kbst", "u1", kb_id="kb_1"))
        status = await kb_svc.get_kb_status(sp.id)
        assert status["bound"] is True
        assert status["kb_id"] == "kb_1"
        assert status["available"] is True

    @pytest.mark.asyncio
    async def test_get_kb_status_not_bound(self, kb_svc, store):
        sp = await store.create_space(_make_space("kbstn", "u1"))
        status = await kb_svc.get_kb_status(sp.id)
        assert status["bound"] is False

    @pytest.mark.asyncio
    async def test_get_kb_status_no_client(self, store, perm):
        svc = SpaceKBService(store, None, perm)
        sp = await store.create_space(_make_space("kbstnc", "u1", kb_id="kb_1"))
        status = await svc.get_kb_status(sp.id)
        assert status["bound"] is True
        assert status["available"] is False


async def _async_gen(chunks: list[str]):
    for c in chunks:
        yield c


# ── M7 Space API ──

class TestSpaceAPI:
    @pytest.fixture
    def mlx_mock(self):
        mock = AsyncMock()
        mock.list_models = AsyncMock(return_value=[{"id": "test-model"}])
        return mock

    @pytest.fixture
    def kb_mock(self):
        mock = AsyncMock()
        mock.list_bases = AsyncMock(return_value=[])
        mock.create_kb = AsyncMock(return_value="kb_api1")
        mock.search = AsyncMock(return_value=[])
        mock.query = AsyncMock(return_value="")
        mock.upload_file = AsyncMock(return_value={"status": "ok"})
        mock.list_documents = AsyncMock(return_value=[])
        mock.health = AsyncMock(return_value=True)
        return mock

    @pytest.fixture
    def api_app(self, store, perm, mlx_mock, kb_mock):
        from fusion_cowork.space.api import create_space_api
        space_svc = SpaceService(store)
        member_svc = SpaceMemberService(store, perm)
        chat_svc = SpaceChatService(store, mlx_mock, perm)
        kb_svc = SpaceKBService(store, kb_mock, perm)
        return create_space_api(space_svc, member_svc, chat_svc, kb_svc)

    @pytest.fixture
    def client(self, api_app):
        from httpx import ASGITransport, AsyncClient
        return AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_health(self, client):
        async with client as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_create_and_get_space(self, client):
        async with client as c:
            resp = await c.post("/spaces", json={"name": "api-test", "owner_id": "u1"})
            assert resp.status_code == 201
            data = resp.json()
            space_id = data["id"]

            resp2 = await c.get(f"/spaces/{space_id}")
            assert resp2.status_code == 200
            assert resp2.json()["name"] == "api-test"

    @pytest.mark.asyncio
    async def test_list_spaces(self, client):
        async with client as c:
            await c.post("/spaces", json={"name": "a", "owner_id": "u1"})
            await c.post("/spaces", json={"name": "b", "owner_id": "u2"})
            resp = await c.get("/spaces")
            assert resp.status_code == 200
            assert len(resp.json()) >= 2

    @pytest.mark.asyncio
    async def test_send_and_list_messages(self, client):
        async with client as c:
            resp = await c.post("/spaces", json={"name": "msg-space", "owner_id": "u1"})
            space_id = resp.json()["id"]
            await c.post(f"/spaces/{space_id}/members",
                         json={"user_id": "u1", "operator_id": "u1", "role": "owner"})
            msg_resp = await c.post(f"/spaces/{space_id}/messages",
                                    json={"user_id": "u1", "content": "hello API"})
            assert msg_resp.status_code == 201
            assert msg_resp.json()["content"] == "hello API"

            list_resp = await c.get(f"/spaces/{space_id}/messages")
            assert list_resp.status_code == 200
            assert len(list_resp.json()) >= 1

    @pytest.mark.asyncio
    async def test_kb_bind_and_status(self, client, kb_mock):
        async with client as c:
            resp = await c.post("/spaces", json={"name": "kb-space", "owner_id": "u1"})
            space_id = resp.json()["id"]
            await c.post(f"/spaces/{space_id}/members",
                         json={"user_id": "u1", "operator_id": "u1", "role": "owner"})

            bind_resp = await c.post(f"/spaces/{space_id}/kb/bind",
                                     json={"operator_id": "u1"})
            assert bind_resp.status_code == 200
            assert bind_resp.json()["kb_id"] == "kb_api1"

            status_resp = await c.get(f"/spaces/{space_id}/kb/status")
            assert status_resp.status_code == 200
            assert status_resp.json()["bound"] is True

    @pytest.mark.asyncio
    async def test_get_space_not_found(self, client):
        async with client as c:
            resp = await c.get("/spaces/nonexistent")
            assert resp.status_code == 404


class TestFusionMLXClientEnhancements:
    """M7.4: FusionMLXClient default port + retry + stream robustness."""

    def test_default_port_is_11432(self):
        from fusion_cowork.ai.mlx_client import DEFAULT_MLX_PORT, FusionMLXClient
        client = FusionMLXClient()
        assert DEFAULT_MLX_PORT == 11432
        assert "11432" in client.base_url

    def test_default_base_url(self):
        from fusion_cowork.ai.mlx_client import DEFAULT_MLX_BASE_URL, FusionMLXClient
        assert DEFAULT_MLX_BASE_URL == "http://localhost:11432/v1"
        client = FusionMLXClient()
        assert client.base_url == "http://localhost:11432/v1"

    def test_custom_base_url_still_works(self):
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(base_url="http://localhost:18000/v1")
        assert client.base_url == "http://localhost:18000/v1"

    def test_retry_params(self):
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(max_retries=3, retry_delay=0.5)
        assert client.max_retries == 3
        assert client.retry_delay == 0.5

    @pytest.mark.asyncio
    async def test_chat_retry_on_connect_error(self):
        import httpx

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(base_url="http://localhost:19999/v1", max_retries=1, retry_delay=0.01)
        with pytest.raises(httpx.ConnectError):
            await client.chat(model="test", messages=[{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_stream_chat_retry_on_connect_error(self):
        import httpx

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(base_url="http://localhost:19999/v1", max_retries=1, retry_delay=0.01)
        chunks = []
        with pytest.raises(httpx.ConnectError):
            async for chunk in client.stream_chat(model="test", messages=[{"role": "user", "content": "hi"}]):
                chunks.append(chunk)
        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        from fusion_cowork.ai.mlx_client import FusionMLXClient
        async with FusionMLXClient(base_url="http://localhost:19999/v1") as client:
            assert client._client is None
        assert client._client is None

    @pytest.mark.asyncio
    async def test_chat_no_retry_on_400(self):
        import httpx

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(max_retries=2, retry_delay=0.01)
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=mock_resp
        )
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_resp)
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(model="test", messages=[{"role": "user", "content": "hi"}])
        assert client._client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_chat_retries_on_503(self):
        import httpx

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        client = FusionMLXClient(max_retries=2, retry_delay=0.01)
        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503
        mock_resp_503.text = "Service Unavailable"
        mock_resp_503.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=mock_resp_503
        )
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_ok.raise_for_status = MagicMock()
        mock_ok.json.return_value = {
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {},
        }
        client._client = MagicMock()
        client._client.post = AsyncMock(side_effect=[mock_resp_503, mock_ok])
        result = await client.chat(model="test", messages=[{"role": "user", "content": "hi"}])
        assert result.content == "hello"
        assert client._client.post.call_count == 2


class TestSharedContext:
    """M7.6: SharedContext for workflow node space context access."""

    def test_create_context(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1")
        assert ctx.space_id == "sp1"
        assert ctx._chat is None
        assert ctx._kb is None

    def test_create_with_services(self):
        from fusion_cowork.space.shared_context import SharedContext
        chat = MagicMock()
        kb = MagicMock()
        ctx = SharedContext(space_id="sp1", chat_service=chat, kb_service=kb, extra={"foo": "bar"})
        assert ctx._chat is chat
        assert ctx._kb is kb
        assert ctx.get_extra("foo") == "bar"

    def test_to_dict(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1", chat_service=MagicMock(), extra={"k": 1})
        d = ctx.to_dict()
        assert d["space_id"] == "sp1"
        assert d["has_chat"] is True
        assert d["has_kb"] is False
        assert "k" in d["extra_keys"]

    def test_from_dict(self):
        from fusion_cowork.space.shared_context import SharedContext
        d = {"space_id": "sp2", "extra": {"x": 1}}
        ctx = SharedContext.from_dict(d)
        assert ctx.space_id == "sp2"
        assert ctx.get_extra("x") == 1

    @pytest.mark.asyncio
    async def test_get_messages_no_service(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1")
        result = await ctx.get_messages()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_messages_with_service(self):
        from fusion_cowork.space.models import SpaceMessage
        from fusion_cowork.space.shared_context import SharedContext
        chat = AsyncMock()
        msg = SpaceMessage(space_id="sp1", user_id="u1", content="hello")
        chat.list_messages = AsyncMock(return_value=[msg])
        ctx = SharedContext(space_id="sp1", chat_service=chat)
        result = await ctx.get_messages()
        assert len(result) == 1
        assert result[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_search_kb_no_service(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1")
        result = await ctx.search_kb("test query")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_kb_with_service(self):
        from fusion_cowork.space.shared_context import SharedContext
        kb = AsyncMock()
        kb.search = AsyncMock(return_value=[{"content": "doc1", "score": 0.9}])
        ctx = SharedContext(space_id="sp1", kb_service=kb)
        result = await ctx.search_kb("query", top_k=3)
        assert len(result) == 1
        kb.search.assert_called_once_with("sp1", "query", top_k=3)

    @pytest.mark.asyncio
    async def test_query_kb(self):
        from fusion_cowork.space.shared_context import SharedContext
        kb = AsyncMock()
        kb.query = AsyncMock(return_value="answer text")
        ctx = SharedContext(space_id="sp1", kb_service=kb)
        result = await ctx.query_kb("question")
        assert result == "answer text"

    @pytest.mark.asyncio
    async def test_get_messages_error_fallback(self):
        from fusion_cowork.space.shared_context import SharedContext
        chat = AsyncMock()
        chat.list_messages = AsyncMock(side_effect=RuntimeError("db error"))
        ctx = SharedContext(space_id="sp1", chat_service=chat)
        result = await ctx.get_messages()
        assert result == []

    def test_inject_and_extract(self):
        from fusion_cowork.space.shared_context import (
            SharedContext,
            extract_shared_context,
            inject_shared_context,
        )
        ctx = SharedContext(space_id="sp1")
        node_input = {"data": "test"}
        inject_shared_context(node_input, ctx)
        assert "_shared_context" in node_input
        extracted = extract_shared_context(node_input)
        assert extracted is ctx
        assert extracted.space_id == "sp1"

    def test_extract_no_context(self):
        from fusion_cowork.space.shared_context import extract_shared_context
        assert extract_shared_context({}) is None
        assert extract_shared_context({"_shared_context": "not_a_context"}) is None

    def test_extra_operations(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1")
        assert ctx.get_extra("missing") is None
        assert ctx.get_extra("missing", 42) == 42
        ctx.set_extra("key", "val")
        assert ctx.get_extra("key") == "val"

    @pytest.mark.asyncio
    async def test_query_kb_no_service(self):
        from fusion_cowork.space.shared_context import SharedContext
        ctx = SharedContext(space_id="sp1")
        result = await ctx.query_kb("q")
        assert result == ""


# ── M8: Agent Runtime + Multi-Agent Relay Tests ──


class TestSpaceAgentRuntime:
    """Test SpaceAgentRuntime CRUD + call + chain."""

    @pytest.fixture
    async def store_and_runtime(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space.agent_runtime import SpaceAgentRuntime
        from fusion_cowork.space.permission import SpacePermission
        from fusion_cowork.space.store import SpaceStore
        store = SpaceStore(data_dir=str(tmp_path / "spaces"))
        await store.initialize()
        perm = SpacePermission(store)
        mlx = MagicMock(spec=FusionMLXClient)
        mlx.chat = AsyncMock()
        mlx.list_models = AsyncMock(return_value=[{"id": "test-model"}])
        rt = SpaceAgentRuntime(store, mlx, perm)
        yield store, rt, mlx
        await store.close()

    @pytest.mark.asyncio
    async def test_add_agent(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp1", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp1", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        result = await rt.add_agent(
            space_id="sp1", operator_id="owner1", name="Writer",
            agent_type="assistant", system_prompt="You write.", enable_rag=True,
        )
        assert result["name"] == "Writer"
        assert result["agent_type"] == "assistant"
        assert result["enable_rag"] is True
        assert "id" in result

    @pytest.mark.asyncio
    async def test_add_agent_permission_denied(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp2", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp2", user_id="viewer1", role=SpaceRole.VIEWER,
                             display_name="viewer", joined_at=now, last_active=now)
        await store.add_member(member)
        with pytest.raises(PermissionError):
            await rt.add_agent(space_id="sp2", operator_id="viewer1", name="X")

    @pytest.mark.asyncio
    async def test_list_agents(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp3", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp3", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        await rt.add_agent(space_id="sp3", operator_id="owner1", name="A1")
        await rt.add_agent(space_id="sp3", operator_id="owner1", name="A2")
        agents = await rt.list_agents("sp3")
        assert len(agents) == 2

    @pytest.mark.asyncio
    async def test_get_agent(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp4", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp4", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        result = await rt.add_agent(space_id="sp4", operator_id="owner1", name="G1")
        fetched = await rt.get_agent("sp4", result["id"])
        assert fetched is not None
        assert fetched["name"] == "G1"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        result = await rt.get_agent("nonexist", "agent_xxx")
        assert result is None

    @pytest.mark.asyncio
    async def test_remove_agent(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp5", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp5", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        result = await rt.add_agent(space_id="sp5", operator_id="owner1", name="R1")
        removed = await rt.remove_agent("sp5", result["id"], "owner1")
        assert removed is True
        fetched = await rt.get_agent("sp5", result["id"])
        assert fetched is None

    @pytest.mark.asyncio
    async def test_remove_agent_not_found(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp5b", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp5b", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        removed = await rt.remove_agent("sp5b", "agent_xxx", "owner1")
        assert removed is False

    @pytest.mark.asyncio
    async def test_call_agent(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.ai.mlx_client import LLMResponse
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp6", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        owner_m = SpaceMember(space_id="sp6", user_id="owner1", role=SpaceRole.OWNER,
                              display_name="owner", joined_at=now, last_active=now)
        await store.add_member(owner_m)
        member = SpaceMember(space_id="sp6", user_id="member1", role=SpaceRole.MEMBER,
                             display_name="member", joined_at=now, last_active=now)
        await store.add_member(member)
        result = await rt.add_agent(space_id="sp6", operator_id="owner1", name="Caller",
                                     system_prompt="You are helpful.")
        agent_id = result["id"]
        mlx.chat.return_value = LLMResponse(content="Hello back!")
        reply = await rt.call_agent("sp6", agent_id, "member1", "Hello")
        assert reply == "Hello back!"
        call_args = mlx.chat.call_args
        assert call_args[1]["messages"][0]["role"] == "system"
        assert call_args[1]["messages"][1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_call_agent_not_found(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp7", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp7", user_id="member1", role=SpaceRole.MEMBER,
                             display_name="member", joined_at=now, last_active=now)
        await store.add_member(member)
        with pytest.raises(ValueError, match="not found"):
            await rt.call_agent("sp7", "nonexist_agent", "member1", "hi")

    @pytest.mark.asyncio
    async def test_call_agent_permission_denied(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp7b", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp7b", user_id="viewer1", role=SpaceRole.VIEWER,
                             display_name="viewer", joined_at=now, last_active=now)
        await store.add_member(member)
        with pytest.raises(PermissionError):
            await rt.call_agent("sp7b", "agent_x", "viewer1", "hi")

    @pytest.mark.asyncio
    async def test_chain_agents(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.ai.mlx_client import LLMResponse
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp8", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp8", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        a1 = await rt.add_agent(space_id="sp8", operator_id="owner1", name="Step1")
        a2 = await rt.add_agent(space_id="sp8", operator_id="owner1", name="Step2")
        mlx.chat.side_effect = [
            LLMResponse(content="step1 output"),
            LLMResponse(content="step2 output"),
        ]
        results = await rt.chain_agents("sp8", [a1["id"], a2["id"]], "owner1", "start")
        assert len(results) == 2
        assert results[0]["content"] == "step1 output"
        assert results[1]["content"] == "step2 output"

    @pytest.mark.asyncio
    async def test_chain_agents_min_two(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp_min2", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        owner_m = SpaceMember(space_id="sp_min2", user_id="owner1", role=SpaceRole.OWNER,
                              display_name="owner", joined_at=now, last_active=now)
        await store.add_member(owner_m)
        with pytest.raises(ValueError, match="at least 2"):
            await rt.chain_agents("sp_min2", ["a1"], "owner1", "msg")

    @pytest.mark.asyncio
    async def test_chain_agents_stops_on_error(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.ai.mlx_client import LLMResponse
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp9", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp9", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        a1 = await rt.add_agent(space_id="sp9", operator_id="owner1", name="Ok")
        a2 = await rt.add_agent(space_id="sp9", operator_id="owner1", name="Fail")
        mlx.chat.side_effect = [
            LLMResponse(content="ok output"),
            RuntimeError("model error"),
        ]
        results = await rt.chain_agents("sp9", [a1["id"], a2["id"]], "owner1", "go")
        assert len(results) == 2
        assert results[0]["content"] == "ok output"
        assert "error" in results[1]

    @pytest.mark.asyncio
    async def test_register_to_orchestrator(self, store_and_runtime):
        store, rt, mlx = store_and_runtime
        from datetime import datetime

        from fusion_cowork.orchestrator.orchestrator import AgentOrchestrator
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        now = datetime.now().isoformat()
        space = Space(id="sp10", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="sp10", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        await rt.add_agent(space_id="sp10", operator_id="owner1", name="OrchA",
                           agent_type="planner")
        orch = AgentOrchestrator()
        count = await rt.register_to_orchestrator("sp10", orch)
        assert count == 1
        assert any("sp10" in aid for aid in orch._agents)


class TestAgentStudioClient:
    """Test AgentStudioClient HTTP client."""

    @pytest.mark.asyncio
    async def test_init(self):
        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient(base_url="http://localhost:9999")
        assert client._base_url == "http://localhost:9999"
        await client.close()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        async with AgentStudioClient() as client:
            assert client._base_url == "http://localhost:8765"

    @pytest.mark.asyncio
    async def test_custom_timeout(self):
        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient(timeout=60.0)
        assert client._timeout == 60.0
        await client.close()

    @pytest.mark.asyncio
    async def test_list_published_agents(self):
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"agents": [{"id": "a1", "name": "Writer"}]}
        client._client.request = AsyncMock(return_value=mock_resp)
        agents = await client.list_published_agents()
        assert len(agents) == 1
        assert agents[0]["name"] == "Writer"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_agent(self):
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": "a1", "name": "Writer", "system_prompt": "Write!"}
        client._client.request = AsyncMock(return_value=mock_resp)
        agent = await client.get_agent("a1")
        assert agent["name"] == "Writer"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self):
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=mock_resp))
        client._client.request = AsyncMock(return_value=mock_resp)
        client._client.request = AsyncMock(side_effect=httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=mock_resp))
        result = await client.get_agent("missing")
        assert result is None
        await client.close()

    @pytest.mark.asyncio
    async def test_import_agent_to_space(self):
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": "studio_a1", "name": "StudioWriter",
            "system_prompt": "Write!", "agent_type": "assistant",
            "enable_rag": True, "config": {"model": "big"},
        }
        client._client.request = AsyncMock(return_value=mock_resp)
        mock_runtime = MagicMock()
        mock_runtime.add_agent = AsyncMock(return_value={
            "id": "agent_new", "name": "StudioWriter",
        })
        result = await client.import_agent_to_space(
            "studio_a1", mock_runtime, "sp1", "owner1",
        )
        assert result["name"] == "StudioWriter"
        mock_runtime.add_agent.assert_called_once()
        await client.close()

    @pytest.mark.asyncio
    async def test_import_agent_with_overrides(self):
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.space.agent_studio_client import AgentStudioClient
        client = AgentStudioClient()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": "a1", "name": "Orig", "system_prompt": "Orig",
            "agent_type": "assistant", "enable_rag": False, "config": {},
        }
        client._client.request = AsyncMock(return_value=mock_resp)
        mock_runtime = MagicMock()
        mock_runtime.add_agent = AsyncMock(return_value={"id": "new", "name": "Override"})
        _result = await client.import_agent_to_space(
            "a1", mock_runtime, "sp1", "owner1",
            overrides={"name": "Override"},
        )
        call_kwargs = mock_runtime.add_agent.call_args[1]
        assert call_kwargs["name"] == "Override"
        await client.close()


class TestSpaceChatRelay:
    """Test SpaceChatService relay_agents method."""

    @pytest.fixture
    async def chat_setup(self, tmp_path):
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from fusion_cowork.ai.mlx_client import FusionMLXClient
        from fusion_cowork.space.chat import SpaceChatService
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        from fusion_cowork.space.permission import SpacePermission
        from fusion_cowork.space.store import SpaceStore
        store = SpaceStore(data_dir=str(tmp_path / "spaces"))
        await store.initialize()
        now = datetime.now().isoformat()
        space = Space(id="relay_sp", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        member = SpaceMember(space_id="relay_sp", user_id="owner1", role=SpaceRole.OWNER,
                             display_name="owner", joined_at=now, last_active=now)
        await store.add_member(member)
        perm = SpacePermission(store)
        mlx = MagicMock(spec=FusionMLXClient)
        mlx.chat = AsyncMock()
        mlx.list_models = AsyncMock(return_value=[{"id": "m1"}])
        chat_svc = SpaceChatService(store, mlx, perm)
        # Add 2 agents
        from fusion_cowork.space.agent_runtime import SpaceAgentRuntime
        rt = SpaceAgentRuntime(store, mlx, perm)
        a1 = await rt.add_agent("relay_sp", "owner1", "Agent1", system_prompt="You are A1")
        a2 = await rt.add_agent("relay_sp", "owner1", "Agent2", system_prompt="You are A2")
        yield store, chat_svc, mlx, a1["id"], a2["id"]
        await store.close()

    @pytest.mark.asyncio
    async def test_relay_agents(self, chat_setup):
        store, chat_svc, mlx, a1, a2 = chat_setup
        from fusion_cowork.ai.mlx_client import LLMResponse
        mlx.chat.side_effect = [
            LLMResponse(content="response from A1"),
            LLMResponse(content="response from A2"),
        ]
        results = await chat_svc.relay_agents(
            "relay_sp", "owner1", [a1, a2], "start message",
        )
        assert len(results) == 2
        assert results[0]["content"] == "response from A1"
        assert results[1]["content"] == "response from A2"

    @pytest.mark.asyncio
    async def test_relay_agents_permission_denied(self, chat_setup):
        store, chat_svc, mlx, a1, a2 = chat_setup
        with pytest.raises(PermissionError):
            await chat_svc.relay_agents(
                "relay_sp", "non_member", [a1, a2], "msg",
            )

    @pytest.mark.asyncio
    async def test_relay_agents_min_two(self, chat_setup):
        store, chat_svc, mlx, a1, a2 = chat_setup
        with pytest.raises(ValueError, match="at least 2"):
            await chat_svc.relay_agents("relay_sp", "owner1", ["a1"], "msg")


class TestCallAgentPermission:
    """Test call_agent permission in SpacePermission matrix."""

    @pytest.fixture
    async def perm_setup(self, tmp_path):
        from datetime import datetime

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        from fusion_cowork.space.permission import SpacePermission
        from fusion_cowork.space.store import SpaceStore
        store = SpaceStore(data_dir=str(tmp_path / "spaces"))
        await store.initialize()
        now = datetime.now().isoformat()
        space = Space(id="perm_sp", name="test", owner_id="owner1",
                      config=SpaceConfig(), created_at=now, updated_at=now)
        await store.create_space(space)
        for role in SpaceRole:
            m = SpaceMember(space_id="perm_sp", user_id=f"{role.value}_user",
                            role=role, display_name=role.value, joined_at=now, last_active=now)
            await store.add_member(m)
        perm = SpacePermission(store)
        yield perm
        await store.close()

    @pytest.mark.asyncio
    async def test_owner_can_call_agent(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm_sp", "owner_user", "call_agent") is True

    @pytest.mark.asyncio
    async def test_admin_can_call_agent(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm_sp", "admin_user", "call_agent") is True

    @pytest.mark.asyncio
    async def test_member_can_call_agent(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm_sp", "member_user", "call_agent") is True

    @pytest.mark.asyncio
    async def test_viewer_cannot_call_agent(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm_sp", "viewer_user", "call_agent") is False


# ── M8.9: Artifact 权限测试 ──


class TestSpaceArtifactService:
    """SpaceArtifactService CRUD + 权限检查。"""

    @pytest.fixture
    async def artifact_setup(self):
        import shutil
        import tempfile

        from fusion_cowork.space.artifact import SpaceArtifactService
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        from fusion_cowork.space.permission import SpacePermission
        from fusion_cowork.space.store import SpaceStore
        d = tempfile.mkdtemp()
        store = SpaceStore(data_dir=d)
        await store.initialize()
        perm = SpacePermission(store)
        svc = SpaceArtifactService(store, perm)
        sp = Space(id="art_sp", name="artifact test", owner_id="owner_u",
                   config=SpaceConfig(), created_at="2026-01-01T00:00:00",
                   updated_at="2026-01-01T00:00:00")
        await store.create_space(sp)
        for uid, role in [("owner_u", "owner"), ("admin_u", "admin"),
                          ("member_u", "member"), ("viewer_u", "viewer")]:
            m = SpaceMember(space_id="art_sp", user_id=uid, role=SpaceRole(role),
                            display_name=uid, joined_at="2026-01-01T00:00:00",
                            last_active="2026-01-01T00:00:00")
            await store.add_member(m)
        yield svc, store
        await store.close()
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_create_artifact_owner(self, artifact_setup):
        svc, _ = artifact_setup
        result = await svc.create_artifact("art_sp", "owner_u", name="doc1")
        assert result["name"] == "doc1"
        assert result["version"] == 1
        assert result["owner_user_id"] == "owner_u"

    @pytest.mark.asyncio
    async def test_create_artifact_member_denied(self, artifact_setup):
        svc, _ = artifact_setup
        with pytest.raises(PermissionError):
            await svc.create_artifact("art_sp", "member_u", name="doc2")

    @pytest.mark.asyncio
    async def test_create_artifact_viewer_denied(self, artifact_setup):
        svc, _ = artifact_setup
        with pytest.raises(PermissionError):
            await svc.create_artifact("art_sp", "viewer_u", name="doc3")

    @pytest.mark.asyncio
    async def test_get_artifact(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        art = await svc.get_artifact("art_sp", created["id"], "member_u")
        assert art is not None
        assert art["name"] == "doc"

    @pytest.mark.asyncio
    async def test_update_artifact_by_owner(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        result = await svc.update_artifact("art_sp", created["id"], "owner_u",
                                           content="updated", name="doc_v2")
        assert result["version"] == 2

    @pytest.mark.asyncio
    async def test_update_artifact_by_member_denied(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        with pytest.raises(PermissionError):
            await svc.update_artifact("art_sp", created["id"], "member_u", content="hack")

    @pytest.mark.asyncio
    async def test_update_artifact_by_admin_allowed(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        result = await svc.update_artifact("art_sp", created["id"], "admin_u", content="admin_edit")
        assert result["version"] == 2

    @pytest.mark.asyncio
    async def test_share_artifact_owner(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        result = await svc.share_artifact("art_sp", created["id"], "owner_u")
        assert "share_code" in result

    @pytest.mark.asyncio
    async def test_share_artifact_member_denied(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        with pytest.raises(PermissionError):
            await svc.share_artifact("art_sp", created["id"], "member_u")

    @pytest.mark.asyncio
    async def test_transfer_ownership(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        result = await svc.transfer_ownership("art_sp", created["id"],
                                              "owner_u", "member_u")
        assert result["new_owner"] == "member_u"

    @pytest.mark.asyncio
    async def test_transfer_by_member_denied(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        with pytest.raises(PermissionError):
            await svc.transfer_ownership("art_sp", created["id"],
                                         "member_u", "viewer_u")

    @pytest.mark.asyncio
    async def test_list_artifacts(self, artifact_setup):
        svc, _ = artifact_setup
        await svc.create_artifact("art_sp", "owner_u", name="a1")
        await svc.create_artifact("art_sp", "owner_u", name="a2")
        arts = await svc.list_artifacts("art_sp", "member_u")
        assert len(arts) >= 2

    @pytest.mark.asyncio
    async def test_delete_artifact_owner(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        removed = await svc.delete_artifact("art_sp", created["id"], "owner_u")
        assert removed is True

    @pytest.mark.asyncio
    async def test_delete_artifact_member_denied(self, artifact_setup):
        svc, _ = artifact_setup
        created = await svc.create_artifact("art_sp", "owner_u", name="doc")
        removed = await svc.delete_artifact("art_sp", created["id"], "member_u")
        assert removed is False


class TestArtifactPermissionActions:
    """15-action 权限矩阵中 artifact 4 动作验证。"""

    @pytest.fixture
    async def perm_setup(self):
        import shutil
        import tempfile

        from fusion_cowork.space.models import Space, SpaceConfig, SpaceMember, SpaceRole
        from fusion_cowork.space.permission import SpacePermission
        from fusion_cowork.space.store import SpaceStore
        d = tempfile.mkdtemp()
        store = SpaceStore(data_dir=d)
        await store.initialize()
        perm = SpacePermission(store)
        sp = Space(id="perm2_sp", name="perm2", owner_id="owner_u",
                   config=SpaceConfig(), created_at="2026-01-01T00:00:00",
                   updated_at="2026-01-01T00:00:00")
        await store.create_space(sp)
        for uid, role in [("owner_u", "owner"), ("admin_u", "admin"),
                          ("member_u", "member"), ("viewer_u", "viewer")]:
            m = SpaceMember(space_id="perm2_sp", user_id=uid, role=SpaceRole(role),
                            display_name=uid, joined_at="2026-01-01T00:00:00",
                            last_active="2026-01-01T00:00:00")
            await store.add_member(m)
        yield perm
        await store.close()
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_view_artifact_all_roles(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm2_sp", "owner_u", "view_artifact") is True
        assert await perm.check("perm2_sp", "member_u", "view_artifact") is True
        assert await perm.check("perm2_sp", "viewer_u", "view_artifact") is True

    @pytest.mark.asyncio
    async def test_edit_artifact_owner_admin_only(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm2_sp", "owner_u", "edit_artifact") is True
        assert await perm.check("perm2_sp", "admin_u", "edit_artifact") is True
        assert await perm.check("perm2_sp", "member_u", "edit_artifact") is False
        assert await perm.check("perm2_sp", "viewer_u", "edit_artifact") is False

    @pytest.mark.asyncio
    async def test_share_artifact_owner_admin_only(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm2_sp", "owner_u", "share_artifact") is True
        assert await perm.check("perm2_sp", "member_u", "share_artifact") is False

    @pytest.mark.asyncio
    async def test_transfer_artifact_owner_admin_only(self, perm_setup):
        perm = perm_setup
        assert await perm.check("perm2_sp", "owner_u", "transfer_artifact") is True
        assert await perm.check("perm2_sp", "viewer_u", "transfer_artifact") is False


# ── M8.10: FSB 模块集成测试 ──


class TestModuleRegistry:
    """ModuleRegistry — 侧边栏模块注册/启用/禁用。"""

    @pytest.fixture
    async def module_setup(self):
        import shutil
        import tempfile

        from fusion_cowork.space.fsb import ModuleRegistry
        from fusion_cowork.space.store import SpaceStore
        d = tempfile.mkdtemp()
        store = SpaceStore(data_dir=d)
        await store.initialize()
        reg = ModuleRegistry(store)
        yield reg, store
        await store.close()
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_register_module(self, module_setup):
        reg, _ = module_setup
        result = await reg.register_module("fsb", "Fusion Small Business",
                                           icon="🏪", route_path="/fsb")
        assert result["id"] == "fsb"
        assert result["name"] == "Fusion Small Business"
        assert result["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_modules(self, module_setup):
        reg, _ = module_setup
        await reg.register_module("fsb", "FSB", icon="🏪", route_path="/fsb")
        await reg.register_module("chat", "Chat", icon="💬", route_path="/chat")
        modules = await reg.list_modules()
        assert len(modules) >= 2

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, module_setup):
        reg, _ = module_setup
        await reg.register_module("fsb", "FSB", enabled=True)
        await reg.register_module("disabled_mod", "Disabled", enabled=False)
        enabled = await reg.list_modules(enabled_only=True)
        ids = [m["id"] for m in enabled]
        assert "fsb" in ids
        assert "disabled_mod" not in ids

    @pytest.mark.asyncio
    async def test_enable_disable_module(self, module_setup):
        reg, _ = module_setup
        await reg.register_module("fsb", "FSB", enabled=True)
        await reg.disable_module("fsb")
        mod = await reg.get_module("fsb")
        assert mod["enabled"] == 0
        await reg.enable_module("fsb")
        mod = await reg.get_module("fsb")
        assert mod["enabled"] == 1

    @pytest.mark.asyncio
    async def test_get_module(self, module_setup):
        reg, _ = module_setup
        await reg.register_module("fsb", "FSB", icon="🏪")
        mod = await reg.get_module("fsb")
        assert mod is not None
        assert mod["icon"] == "🏪"

    @pytest.mark.asyncio
    async def test_get_module_not_found(self, module_setup):
        reg, _ = module_setup
        mod = await reg.get_module("nonexistent")
        assert mod is None


class TestNotificationService:
    """NotificationService — 通知推送与订阅。"""

    @pytest.fixture
    async def notif_setup(self):
        import shutil
        import tempfile

        from fusion_cowork.space.fsb import NotificationService
        from fusion_cowork.space.store import SpaceStore
        d = tempfile.mkdtemp()
        store = SpaceStore(data_dir=d)
        await store.initialize()
        svc = NotificationService(store)
        yield svc, store
        await store.close()
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_push_notification(self, notif_setup):
        svc, _ = notif_setup
        result = await svc.push_notification(
            space_id="sp1", user_id="user1",
            notification_type="approval", title="待审批任务",
        )
        assert result["type"] == "approval"
        assert result["title"] == "待审批任务"

    @pytest.mark.asyncio
    async def test_list_notifications(self, notif_setup):
        svc, _ = notif_setup
        await svc.push_notification("sp1", "user1", "approval", "Task 1")
        await svc.push_notification("sp1", "user1", "approval", "Task 2")
        notifs = await svc.list_notifications("user1")
        assert len(notifs) >= 2

    @pytest.mark.asyncio
    async def test_list_unread_only(self, notif_setup):
        svc, _ = notif_setup
        n = await svc.push_notification("sp1", "user1", "approval", "Unread")
        await svc.mark_read(n["id"])
        await svc.push_notification("sp1", "user1", "approval", "Still unread")
        unread = await svc.list_notifications("user1", unread_only=True)
        assert len(unread) == 1
        assert unread[0]["title"] == "Still unread"

    @pytest.mark.asyncio
    async def test_mark_read(self, notif_setup):
        svc, _ = notif_setup
        n = await svc.push_notification("sp1", "user1", "approval", "Test")
        await svc.mark_read(n["id"])
        notifs = await svc.list_notifications("user1")
        assert notifs[0]["read"] == 1

    @pytest.mark.asyncio
    async def test_subscribe_receive(self, notif_setup):
        svc, _ = notif_setup
        queue = svc.subscribe("user1")
        await svc.push_notification("sp1", "user1", "approval", "Push test")
        assert len(queue) == 1
        assert queue[0]["title"] == "Push test"
        svc.unsubscribe("user1", queue)
