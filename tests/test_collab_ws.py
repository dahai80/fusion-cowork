from __future__ import annotations

import asyncio
import json

from fusion_cowork.server.collab_ws import CollabHub
from fusion_cowork.space.presence import PresenceManager


class _MockWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload):
        self.sent.append(payload)


def _emitter():
    class _E:
        def create_event(self, *a, **k):
            pass

    return _E()


async def _two_members(hub, room="s1"):
    ws1, ws2 = _MockWS(), _MockWS()
    await hub.join(ws1, room, "u1")
    await hub.join(ws2, room, "u2")
    return ws1, ws2


def test_join_and_room_size():
    async def run():
        hub = CollabHub(presence_manager=PresenceManager(event_emitter=_emitter()))
        ws = _MockWS()
        res = await hub.join(ws, "s1", "u1", "Alice")
        assert res["space_id"] == "s1"
        assert hub.room_size("s1") == 1
        assert hub.total_connections() == 1
        joined = json.loads(ws.sent[0])
        assert joined["type"] == "joined"

    asyncio.run(run())


def test_chat_broadcasts_to_room():
    async def run():
        hub = CollabHub()
        ws1, ws2 = await _two_members(hub)
        ws1.sent.clear()
        ws2.sent.clear()
        res = await hub.handle_message(ws1, json.dumps({"type": "chat_send", "content": "hello"}))
        assert res["ok"] is True
        assert any(json.loads(p)["type"] == "chat" for p in ws2.sent)
        assert any(json.loads(p)["type"] == "chat" for p in ws1.sent)

    asyncio.run(run())


def test_cursor_broadcasts_excluding_sender():
    async def run():
        hub = CollabHub(presence_manager=PresenceManager(event_emitter=_emitter()))
        ws1, ws2 = await _two_members(hub)
        ws1.sent.clear()
        ws2.sent.clear()
        await hub.handle_message(ws1, json.dumps({"type": "cursor_move", "x": 5, "y": 6, "target": "art1"}))
        assert any(json.loads(p)["type"] == "cursor" for p in ws2.sent)
        assert not any(json.loads(p)["type"] == "cursor" for p in ws1.sent)

    asyncio.run(run())


def test_ping_pong():
    async def run():
        hub = CollabHub()
        ws = _MockWS()
        await hub.join(ws, "s1", "u1")
        ws.sent.clear()
        res = await hub.handle_message(ws, json.dumps({"type": "ping"}))
        assert res is None
        assert any(json.loads(p)["type"] == "pong" for p in ws.sent)

    asyncio.run(run())


def test_leave_removes_from_room_and_broadcasts():
    async def run():
        hub = CollabHub()
        ws1, ws2 = await _two_members(hub)
        ws2.sent.clear()
        await hub.leave(ws1)
        assert hub.room_size("s1") == 1
        assert any(json.loads(p)["type"] == "member_leave" for p in ws2.sent)

    asyncio.run(run())


def test_handle_message_without_join_errors():
    async def run():
        hub = CollabHub()
        res = await hub.handle_message(_MockWS(), json.dumps({"type": "ping"}))
        assert "error" in res

    asyncio.run(run())


def test_invalid_json_returns_error():
    async def run():
        hub = CollabHub()
        ws = _MockWS()
        await hub.join(ws, "s1", "u1")
        res = await hub.handle_message(ws, "not json{")
        assert "error" in res

    asyncio.run(run())


def test_unknown_message_type_errors():
    async def run():
        hub = CollabHub()
        ws = _MockWS()
        await hub.join(ws, "s1", "u1")
        res = await hub.handle_message(ws, json.dumps({"type": "fly"}))
        assert "error" in res

    asyncio.run(run())


def test_chat_empty_content_errors():
    async def run():
        hub = CollabHub()
        ws = _MockWS()
        await hub.join(ws, "s1", "u1")
        res = await hub.handle_message(ws, json.dumps({"type": "chat_send", "content": "   "}))
        assert "error" in res

    asyncio.run(run())


def test_member_join_broadcast_excludes_joiner():
    async def run():
        hub = CollabHub()
        ws1 = _MockWS()
        await hub.join(ws1, "s1", "u1")
        ws1.sent.clear()
        ws2 = _MockWS()
        await hub.join(ws2, "s1", "u2", "Bob")
        join_events = [json.loads(p) for p in ws1.sent if json.loads(p)["type"] == "member_join"]
        assert len(join_events) == 1
        assert join_events[0]["user_id"] == "u2"
        assert not any(json.loads(p)["type"] == "member_join" for p in ws2.sent)

    asyncio.run(run())


def test_isolated_rooms():
    async def run():
        hub = CollabHub()
        ws1, ws2 = _MockWS(), _MockWS()
        await hub.join(ws1, "s1", "u1")
        await hub.join(ws2, "s2", "u2")
        ws1.sent.clear()
        ws2.sent.clear()
        await hub.handle_message(ws1, json.dumps({"type": "chat_send", "content": "hi"}))
        assert any(json.loads(p)["type"] == "chat" for p in ws1.sent)
        assert not ws2.sent

    asyncio.run(run())


def test_chat_persists_via_chat_svc():
    calls = []

    class _ChatSvc:
        async def send_message(self, space_id, user_id, content):
            calls.append((space_id, user_id, content))

    async def run():
        hub = CollabHub(chat_svc=_ChatSvc())
        ws = _MockWS()
        await hub.join(ws, "s1", "u1")
        await hub.handle_message(ws, json.dumps({"type": "chat_send", "content": "persist me"}))
        assert calls == [("s1", "u1", "persist me")]

    asyncio.run(run())


def test_chat_svc_failure_still_broadcasts():
    class _BadChat:
        async def send_message(self, *a, **k):
            raise RuntimeError("db down")

    async def run():
        hub = CollabHub(chat_svc=_BadChat())
        ws1, ws2 = await _two_members(hub)
        ws2.sent.clear()
        res = await hub.handle_message(ws1, json.dumps({"type": "chat_send", "content": "ok"}))
        assert res["ok"] is True
        assert any(json.loads(p)["type"] == "chat" for p in ws2.sent)

    asyncio.run(run())
