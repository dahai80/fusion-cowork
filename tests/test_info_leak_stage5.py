"""Stage 5 — error handling / information-leak tests (HI-5/13/14/15/4).

- HI-5: trace_id returned in responses, str(e)/stack never leaked to clients (desk_rpc + rpc_bridge + mcp_http + mcp_transport)
- HI-13: collab 128-bit session_id + content <=16KiB + control-char stripping + principal binding
- HI-14: space.update handler field whitelist (rejects owner_id/status hijack) + session metadata <=64KiB
- HI-15: syncKnowledge filename sanitization + base64/decoded size caps + extension whitelist
- HI-4: rpc_bridge params schema validation before dispatch (missing plugin_id → -32602)
"""

from __future__ import annotations

import base64
import json
import re

import pytest

# Trigger node registration (prevent cross-test pollution)
import fusion_cowork.nodes.io.file_io
import fusion_cowork.nodes.tools.tool_nodes  # noqa: F401
from fusion_cowork.engine.node import NodeRegistry


@pytest.fixture(autouse=True)
def _ensure_nodes_loaded():
    if "shell_exec" not in NodeRegistry._registry:
        import fusion_cowork.nodes

        fusion_cowork.nodes.import_all_nodes()


# ── HI-5: trace_id does not leak internal stack ──


class TestHI5TraceIdNoLeak:
    # HI-5: handler exception → client gets only trace_id + generic message, no str(e)/stack/absolute paths

    @pytest.mark.asyncio
    async def test_dispatch_handler_exception_returns_trace_id_not_str(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        server = DeskRPCServer()

        # Inject a handler that always raises
        async def _boom(params):
            raise RuntimeError("secret /etc/passwd SQL error http://internal:8080")

        server._handlers["desk.boom"] = _boom
        resp = await server._dispatch({"jsonrpc": "2.0", "id": 7, "method": "desk.boom", "params": {}})
        err = resp["error"]
        assert err["code"] == -32603
        assert err["message"] == "Internal error"
        tid = err["data"]["trace_id"]
        # v0.4.0 Stage 4 unified trace_id format: fc_<16hex> (observability.trace)
        assert isinstance(tid, str) and len(tid) == 19
        assert re.fullmatch(r"fc_[0-9a-f]{16}", tid)
        # Never leak internal details
        blob = json.dumps(resp, ensure_ascii=False)
        assert "secret" not in blob
        assert "/etc/passwd" not in blob
        assert "SQL error" not in blob
        assert "internal:8080" not in blob

    @pytest.mark.asyncio
    async def test_internal_error_helper_no_str_leak(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        server = DeskRPCServer()
        res = server._internal_error(FileNotFoundError("/Users/dahai/secret/path.db"), method="desk.x")
        assert "trace_id" in res and isinstance(res["trace_id"], str) and len(res["trace_id"]) == 19
        blob = json.dumps(res, ensure_ascii=False)
        # Absolute paths + exception type names must not reach the response
        assert "/Users/dahai" not in blob
        assert "FileNotFoundError" not in blob
        assert "secret/path" not in blob

    @pytest.mark.asyncio
    async def test_rpc_bridge_dispatch_exception_returns_trace_id(self):
        # Trigger an exception inside handler.handle (within the HI-5 try/except), not in get_plugins_handler
        from fusion_cowork.server import rpc_bridge

        orig_get = rpc_bridge.get_plugins_handler

        class _FakeHandler:
            async def handle(self, request):
                raise RuntimeError("internal db://10.0.0.5:5432 leak")

        def _fake():
            return _FakeHandler()

        rpc_bridge.get_plugins_handler = _fake
        rpc_bridge._DEFAULTS_MOUNTED = True  # skip _mount_defaults (needs lifecycle)
        try:
            resp = await rpc_bridge.dispatch_rpc({"jsonrpc": "2.0", "id": 3, "method": "plugins/list", "params": {}})
        finally:
            rpc_bridge.get_plugins_handler = orig_get
            rpc_bridge._DEFAULTS_MOUNTED = False
        err = resp["error"]
        assert err["code"] == -32603
        assert err["message"] == "Internal error"
        tid = err["data"]["trace_id"]
        assert isinstance(tid, str) and re.fullmatch(r"fc_[0-9a-f]{16}", tid)
        blob = json.dumps(resp, ensure_ascii=False)
        assert "db://10.0.0.5" not in blob
        assert "leak" not in blob

    @pytest.mark.asyncio
    async def test_mcp_http_legacy_500_returns_trace_id(self):
        from httpx import ASGITransport, AsyncClient

        from fusion_cowork.server.mcp_http import create_http_app
        from fusion_cowork.server.mcp_server import MCPToolRegistry

        registry = MCPToolRegistry()

        # call_tool's internal try/except swallows tool exceptions; monkeypatch call_tool to raise directly,
        # triggering the mcp_http handler-layer HI-5 catch (bypassing the registry's internal catch)
        async def _boom_call(tool_name, arguments):
            raise RuntimeError("secret stack http://10.0.0.1")

        registry.call_tool = _boom_call
        app = create_http_app(registry)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Initialize first (legacy /mcp requires _initialized)
            await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            await client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialized", "params": {}})
            r = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "boom", "arguments": {}}},
            )
        assert r.status_code == 500
        body = r.json()
        err = body["error"]
        assert err["code"] == -32603
        assert err["message"] == "Internal error"
        tid = err["data"]["trace_id"]
        assert isinstance(tid, str) and re.fullmatch(r"fc_[0-9a-f]{16}", tid)
        blob = json.dumps(body, ensure_ascii=False)
        assert "secret stack" not in blob
        assert "10.0.0.1" not in blob


# ── HI-13: collab sessions ──


class TestHI13CollabHardening:
    # HI-13: collab 128-bit session_id + content <=16KiB + control-char stripping + principal-bound identity

    @pytest.mark.asyncio
    async def test_collab_join_generates_128bit_session_id(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_collab",
            name="c",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        # Attacker tries to impersonate via user_id="admin"
        res = await server._handle_space_collab_join({"space_id": "sp_collab", "user_id": "attacker_admin"})
        assert "error" not in res
        sid = res["session_id"]
        # 128-bit: sess_ + 32 hex chars (token_hex(16))
        assert sid.startswith("sess_")
        hex_part = sid[len("sess_") :]
        assert len(hex_part) == 32 and re.fullmatch(r"[0-9a-f]{32}", hex_part)
        # Principal binds to the authenticated identity local_user, not the attacker-injected admin
        sess = server._collab_sessions[sid]
        assert sess["principal"] == "local_user"
        assert sess["user_id"] == "local_user"
        assert res["user_id"] == "local_user"
        await store.close()

    @pytest.mark.asyncio
    async def test_collab_join_uses_client_session_id_if_present(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_c2",
            name="c2",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        client_sid = "sess_" + "a" * 32
        res = await server._handle_space_collab_join({"space_id": "sp_c2", "session_id": client_sid})
        assert res["session_id"] == client_sid
        await store.close()

    @pytest.mark.asyncio
    async def test_collab_send_rejects_oversized_content(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_c3",
            name="c3",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        join = await server._handle_space_collab_join({"space_id": "sp_c3"})
        sid = join["session_id"]
        # Over 16KiB (16384) → rejected
        big = "x" * (16 * 1024 + 1)
        res = await server._handle_space_collab_send({"session_id": sid, "content": big})
        assert res["error"].startswith("消息超")  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_collab_send_strips_control_chars(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_c4",
            name="c4",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        join = await server._handle_space_collab_join({"space_id": "sp_c4"})
        sid = join["session_id"]
        # Contains C0 control chars (keep \t\n\r, strip \x00\x07\x1b)
        dirty = "hello\x00world\x07ansi\x1b[2J\tok\nline"
        res = await server._handle_space_collab_send({"session_id": sid, "content": dirty})
        assert res.get("ok") is True
        # Fetch back from hub broadcast events (poll), verify control chars stripped
        events = await server._handle_space_collab_poll({"session_id": sid})
        chat = next((e for e in events["events"] if e.get("type") == "chat"), None)
        assert chat is not None
        assert "\x00" not in chat["content"]
        assert "\x07" not in chat["content"]
        assert "\x1b" not in chat["content"]
        assert "\t" in chat["content"]  # tab preserved
        assert "\n" in chat["content"]  # newline preserved
        await store.close()

    @pytest.mark.asyncio
    async def test_collab_send_rejects_non_string_content(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        server = DeskRPCServer()
        server._collab_sessions["sess_fake"] = {"space_id": "sp", "user_id": "u", "ws": object()}
        res = await server._handle_space_collab_send({"session_id": "sess_fake", "content": 12345})
        assert res["error"] == "content 必须为字符串"  # i18n: user-facing


# ── HI-14: space.update whitelist + session metadata cap ──


class TestHI14UpdateWhitelist:
    # HI-14: handler-layer _USER_EDITABLE={name,description} rejects owner_id/status hijack; session metadata <=64KiB

    @pytest.mark.asyncio
    async def test_space_update_rejects_owner_id_hijack(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h14a",
            name="orig",
            description="d",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        # Attacker tries to take over: owner_id→attacker + status→ARCHIVED unarchive
        res = await server._handle_space_update(
            {
                "space_id": "sp_h14a",
                "updates": {"owner_id": "attacker", "status": "archived", "name": "hijacked"},
            }
        )
        # Only name takes effect
        assert res["name"] == "hijacked"
        after = await store.get_space("sp_h14a")
        assert after.owner_id == "local_user"  # ownership not hijacked
        assert after.status == SpaceStatus.ACTIVE  # not unarchived
        await store.close()

    @pytest.mark.asyncio
    async def test_space_update_only_name_description_applied(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h14b",
            name="n",
            description="old",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        res = await server._handle_space_update(
            {
                "space_id": "sp_h14b",
                "updates": {"name": "new", "description": "newdesc"},
            }
        )
        assert res["name"] == "new"
        assert res["description"] == "newdesc"
        await store.close()

    @pytest.mark.asyncio
    async def test_space_update_all_non_whitelist_rejected(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h14c",
            name="n",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        # All high-risk fields → nothing updatable
        res = await server._handle_space_update(
            {
                "space_id": "sp_h14c",
                "updates": {"owner_id": "x", "status": "z", "kb_id": "k", "collab_mode": "m"},
            }
        )
        assert res["error"].startswith("无可更新字段")  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_session_update_rejects_oversized_metadata(self, tmp_path):
        from fusion_cowork.engine.session import Session, SessionStore
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        store = SessionStore(db_path=str(tmp_path / "s.db"))
        sess = Session(workflow_id="wf", workflow_name="w")
        store.save(sess)
        server = DeskRPCServer()
        server._session_store = store
        # Stuff oversized metadata (>64KiB)
        big = {"k": "v" * (70 * 1024)}
        res = await server._handle_session_update({"session_id": sess.id, "updates": {"metadata": big}})
        assert res["error"].startswith("metadata 超")  # i18n: user-facing
        # Confirm it was not written
        after = store.get(sess.id)
        assert "k" not in after.metadata

    @pytest.mark.asyncio
    async def test_session_update_accepts_normal_metadata(self, tmp_path):
        from fusion_cowork.engine.session import Session, SessionStore
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        store = SessionStore(db_path=str(tmp_path / "s2.db"))
        sess = Session(workflow_id="wf", workflow_name="w")
        store.save(sess)
        server = DeskRPCServer()
        server._session_store = store
        res = await server._handle_session_update({"session_id": sess.id, "updates": {"metadata": {"note": "ok"}}})
        assert res["metadata"]["note"] == "ok"


# ── HI-15: syncKnowledge file sanitization ──


class TestHI15SyncKnowledgeSanitize:
    # HI-15: syncKnowledge rejects path traversal + base64 size cap + extension whitelist + decoded size cap
    # All validation gates run before kb_svc.upload_document (needs network); rejections land in errors[] without network

    @pytest.mark.asyncio
    async def test_sync_knowledge_neutralizes_path_traversal(self):
        # _secure_filename strips ../ keeping the basename: "../../etc/cron.d/x.txt" → "x.txt"
        # Traversal is neutralized (never enters the KB as a malicious path) rather than crudely rejected
        from fusion_cowork.server.desk_rpc import _secure_filename

        assert _secure_filename("../../etc/cron.d/x.txt") == "x.txt"
        assert _secure_filename("../../../home/user/notes.txt") == "notes.txt"
        # Pure traversal / empty name → rejected (returns empty → handler rejects)
        assert _secure_filename("../../") == ""
        assert _secure_filename("") == ""
        assert _secure_filename("...") == ""

    @pytest.mark.asyncio
    async def test_sync_knowledge_rejects_empty_name(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h15",
            name="k",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        content = base64.b64encode(b"hello").decode()
        # Empty name → _secure_filename returns empty → handler reports the invalid-or-missing-filename error (no network)
        res = await server._handle_project_sync_knowledge(
            {
                "space_id": "sp_h15",
                "files": [{"name": "", "content": content}],
            }
        )
        assert res["synced"] == []
        assert len(res["errors"]) == 1
        assert "非法" in res["errors"][0]["error"] or "缺失" in res["errors"][0]["error"]  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_sync_knowledge_rejects_non_text_extension(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h15b",
            name="k",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        content = base64.b64encode(b"MZbinary").decode()
        res = await server._handle_project_sync_knowledge(
            {
                "space_id": "sp_h15b",
                "files": [{"name": "malware.exe", "content": content}],
            }
        )
        assert res["synced"] == []
        assert len(res["errors"]) == 1
        assert "非文本扩展名" in res["errors"][0]["error"]  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_sync_knowledge_rejects_oversized_b64(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h15c",
            name="k",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        # Build >50MiB of base64 (padded with 'A', a valid b64 char)
        big_b64 = "A" * (50 * 1024 * 1024 + 100)
        res = await server._handle_project_sync_knowledge(
            {
                "space_id": "sp_h15c",
                "files": [{"name": "big.txt", "content": big_b64}],
            }
        )
        assert res["synced"] == []
        assert len(res["errors"]) == 1
        assert "base64 超" in res["errors"][0]["error"]  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_sync_knowledge_rejects_invalid_b64(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h15d",
            name="k",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        # Invalid base64 (validate=True raises)
        res = await server._handle_project_sync_knowledge(
            {
                "space_id": "sp_h15d",
                "files": [{"name": "bad.txt", "content": "!!!not-base64@@@"}],
            }
        )
        assert res["synced"] == []
        assert len(res["errors"]) == 1
        # decode exception → upload-failed error + trace_id (no str(e) leak)
        assert res["errors"][0]["error"] == "上传失败"  # i18n: user-facing
        assert "trace_id" in res["errors"][0]
        await store.close()

    @pytest.mark.asyncio
    async def test_sync_knowledge_rejects_too_many_files(self, tmp_path):
        from datetime import UTC, datetime

        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_h15e",
            name="k",
            description="",
            owner_id="local_user",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        server = DeskRPCServer(space_store=store)
        files = [{"name": f"f{i}.txt", "content": base64.b64encode(b"x").decode()} for i in range(51)]
        res = await server._handle_project_sync_knowledge({"space_id": "sp_h15e", "files": files})
        assert "不超" in res["error"]  # i18n: user-facing
        await store.close()


# ── HI-4: rpc_bridge params validation ──


class TestHI4RpcBridgeParamSchema:
    # HI-4: dispatch_rpc validates params schema before dispatch, missing plugin_id → -32602, never reaches handler

    @pytest.mark.asyncio
    async def test_install_missing_plugin_id_rejected(self):
        from fusion_cowork.server.rpc_bridge import dispatch_rpc

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 1, "method": "plugins/install", "params": {}})
        assert resp["error"]["code"] == -32602
        assert "plugin_id" in str(resp["error"]["data"]["missing"])

    @pytest.mark.asyncio
    async def test_uninstall_missing_plugin_id_rejected(self):
        from fusion_cowork.server.rpc_bridge import dispatch_rpc

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 2, "method": "plugins/uninstall", "params": {"x": 1}})
        assert resp["error"]["code"] == -32602
        assert resp["error"]["data"]["method"] == "plugins/uninstall"

    @pytest.mark.asyncio
    async def test_state_get_missing_plugin_id_rejected(self):
        from fusion_cowork.server.rpc_bridge import dispatch_rpc

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 3, "method": "plugins/state.get", "params": "not-a-dict"})
        # params not a dict → -32602
        assert resp["error"]["code"] == -32602
        assert "非对象" in resp["error"]["message"] or "缺失" in resp["error"]["message"]  # i18n: user-facing

    @pytest.mark.asyncio
    async def test_config_get_no_required_params_passes_schema(self):
        # config.get has no required fields → passes schema validation, reaches handler (deps present → handler response; absent → -32603)
        from fusion_cowork.server.rpc_bridge import dispatch_rpc, is_plugins_available

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 4, "method": "plugins/config.get", "params": {}})
        # Must not be blocked by schema (not -32602)
        if resp.get("error", {}).get("code") == -32602:
            raise AssertionError("config.get 被 schema 误拦")  # i18n: user-facing
        # Reached the handler layer (deps present → has result; absent → -32603 install hint)
        if is_plugins_available():
            assert "result" in resp or resp.get("error", {}).get("code") != -32602
        else:
            assert resp["error"]["code"] == -32603

    @pytest.mark.asyncio
    async def test_install_with_plugin_id_passes_schema(self):
        # With a valid plugin_id → passes schema, reaches handler layer
        from fusion_cowork.server.rpc_bridge import dispatch_rpc, is_plugins_available

        resp = await dispatch_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "plugins/install",
                "params": {"plugin_id": "caveman_compress"},
            }
        )
        # Must not be -32602 (schema passes)
        assert resp.get("error", {}).get("code") != -32602
        # Reaches handler: deps present → result or business error; absent → -32603
        if not is_plugins_available():
            assert resp["error"]["code"] == -32603
