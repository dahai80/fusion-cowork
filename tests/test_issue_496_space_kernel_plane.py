# issue #496: space chat message plane re-pointed to the kernel TeamMessage
# runtime (chat-standardization §6-C4).
#
# Gates under test:
# - E2E @ mention routes to the target member's kernel inbox (NOT the
#   space-internal chat table — no new store rows)
# - reply threading via reply_to
# - attachment ref-only rule (artifact refs, non-strings dropped)
# - kernel member validation (@unknown mention fails loudly)
# - legacy chat rows stay readable (dual-read transition, roadmap §7.3)
# - history survives a plane rebuild (kernel inbox files restored)
# - member/presence/shared_context public APIs unchanged

from datetime import UTC

import pytest
import pytest_asyncio

from fusion_cowork.space.kernel_plane import SpaceKernelPlane
from fusion_cowork.space.models import Space, SpaceMember, SpaceMessage, SpaceRole
from fusion_cowork.space.store import SpaceStore

STATIC_KEY = "test-key-496"


def _make_space(name="s496", owner_id="u1"):
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    return Space(id=f"sp_{name}", name=name, description="", owner_id=owner_id, created_at=now, updated_at=now)


def _make_member(space_id, user_id, role=SpaceRole.OWNER):
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    return SpaceMember(
        space_id=space_id, user_id=user_id, role=role, display_name=user_id, joined_at=now, last_active=now
    )


@pytest_asyncio.fixture
async def env(tmp_path):
    store = SpaceStore(data_dir=str(tmp_path / "data"))
    await store.initialize()
    sp = await store.create_space(_make_space())
    sid = sp.id
    await store.add_member(_make_member(sid, "u1"))
    await store.add_agent({"id": "agent_a", "name": "A1", "system_prompt": "", "space_id": sid})
    plane = SpaceKernelPlane(store, state_root=str(tmp_path / "teams"))
    return store, plane, sid


def _bodies(msgs):
    return [m.content for m in msgs]


class TestMentionRouting:
    @pytest.mark.asyncio
    async def test_mention_lands_in_target_kernel_inbox_not_store(self, env):
        store, plane, sid = env
        msg = await plane.send(sid, sender_id="u1", sender_is_agent=False, content="hello @agent_a check this")
        assert msg.id.startswith("msg_")
        inbox = await plane.inbox(sid, "agent_a")
        assert any(r["msg_id"] == msg.id and r["body"] == "hello @agent_a check this" for r in inbox)
        # no space-internal chat table row was written
        legacy = await store.get_messages(sid, limit=100)
        assert all(m.id != msg.id for m in legacy)

    @pytest.mark.asyncio
    async def test_unknown_mention_fails_kernel_validation(self, env):
        _, plane, sid = env
        with pytest.raises(ValueError, match="unknown mention @ghost"):
            await plane.send(sid, sender_id="u1", sender_is_agent=False, content="ping @ghost")

    @pytest.mark.asyncio
    async def test_broadcast_single_msg_id_all_members(self, env):
        _, plane, sid = env
        msg = await plane.send(sid, sender_id="u1", sender_is_agent=False, content="all hands")
        a_inbox = await plane.inbox(sid, "agent_a")
        assert all(r["msg_id"] == msg.id for r in a_inbox)
        history = await plane.history(sid)
        assert _bodies(history).count("all hands") == 1


class TestThreadingAndAttachments:
    @pytest.mark.asyncio
    async def test_reply_threading_reply_to(self, env):
        _, plane, sid = env
        parent = await plane.send(sid, sender_id="u1", sender_is_agent=False, content="root")
        reply = await plane.send(
            sid, sender_id="agent_a", sender_is_agent=True, content="answer", parent_msg_id=parent.id
        )
        assert reply.parent_msg_id == parent.id
        history = await plane.history(sid)
        by_id = {m.id: m for m in history}
        assert by_id[reply.id].parent_msg_id == parent.id

    @pytest.mark.asyncio
    async def test_attachment_ref_only(self, env, caplog):
        _, plane, sid = env
        ref = "art_ab12cd34"
        msg = await plane.send(
            sid,
            sender_id="u1",
            sender_is_agent=False,
            content="see file",
            attachments=[ref, {"bad": "dict"}, ""],
        )
        inbox = await plane.inbox(sid, "agent_a")
        rec = next(r for r in inbox if r["msg_id"] == msg.id)
        assert list(rec["attachments"]) == [ref]
        assert any("non-ref attachment" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_attachment_ref_resolves_via_artifact_read(self, env):
        store, plane, sid = env
        from fusion_cowork.space.artifact import SpaceArtifactService
        from fusion_cowork.space.permission import SpacePermission

        perm = SpacePermission(store)
        art_svc = SpaceArtifactService(store, perm)
        created = await art_svc.create_artifact(sid, "u1", name="spec", content="attachment body")
        art_id = created["id"] if isinstance(created, dict) else created.id
        msg = await plane.send(sid, sender_id="u1", sender_is_agent=False, content="doc attached", attachments=[art_id])
        row = await art_svc.get_artifact(sid, art_id, "u1")
        assert row is not None
        history = await plane.history(sid)
        sent = next(m for m in history if m.id == msg.id)
        assert sent.attachments == [art_id]


class TestDualReadAndPersistence:
    @pytest.mark.asyncio
    async def test_legacy_rows_still_readable(self, env):
        store, plane, sid = env
        legacy = SpaceMessage(space_id=sid, user_id="u1", content="pre-migration row")
        await store.add_message(legacy)
        history = await plane.history(sid)
        assert all(m.id != legacy.id for m in history)
        from fusion_cowork.space.chat import SpaceChatService

        svc = SpaceChatService(
            store,
            mlx_client=None,
            permission=None,
            plane=plane,  # type: ignore[arg-type]
        )
        merged = await svc.list_messages(sid)
        assert any(m.id == legacy.id for m in merged)

    @pytest.mark.asyncio
    async def test_history_survives_plane_rebuild(self, env):
        store, plane, sid = env
        msg = await plane.send(sid, sender_id="u1", sender_is_agent=False, content="persist me")
        rebuilt = SpaceKernelPlane(store, state_root=plane._state_root)
        history = await rebuilt.history(sid)
        assert any(m.id == msg.id and m.content == "persist me" for m in history)


class TestPublicApisUnchanged:
    @pytest.mark.asyncio
    async def test_member_agent_presence_apis_intact(self, env):
        store, plane, sid = env
        members = await store.list_members(sid)
        assert [m.user_id for m in members] == ["u1"]
        agents = await store.list_agents(sid)
        assert agents[0]["id"] == "agent_a"
        # plane inbox read does not mutate roster
        await plane.inbox(sid, "agent_a")
        assert len(await store.list_members(sid)) == 1
