"""Stage 1 auth baseline tests — CR-1/CR-5/HI-1/2/HI-3/12.

- CR-1: DeskRPC UDS auth token validation + identity field stripping
- CR-5: IDOR denial (caller-supplied identity stripped; space access guard)
- HI-1/2: MCP HTTP/streamable Bearer auth (mcp.auth_token)
- HI-3: RemoteControlServer TLS fail-closed
- HI-12: CrossDeviceSync WS token validation + inbound TLS fail-closed
"""

import pytest

from fusion_cowork.config_center import ConfigCenter
from fusion_cowork.server.mcp_http import create_http_app, create_streamable_app
from fusion_cowork.server.mcp_server import MCPToolRegistry


@pytest.fixture(autouse=True)
def _clean_config():
    cfg = ConfigCenter.get_instance()
    cfg.delete("mcp.auth_token")
    cfg.delete("desk.auth_token")
    yield
    cfg.delete("mcp.auth_token")
    cfg.delete("desk.auth_token")


# -- HI-1/2: MCP HTTP Bearer auth --


class TestMCPHttpAuth:
    def _client(self, token=None):
        from starlette.testclient import TestClient

        if token is not None:
            ConfigCenter.get_instance().set("mcp.auth_token", token)
        registry = MCPToolRegistry()
        registry.register_tools()
        app = create_http_app(registry)
        return TestClient(app)

    def test_no_token_no_auth(self):
        client = self._client()
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp.status_code == 200

    def test_token_missing_returns_401(self):
        client = self._client(token="secret")
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp.status_code == 401

    def test_token_wrong_returns_401(self):
        client = self._client(token="secret")
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_token_correct_passes(self):
        client = self._client(token="secret")
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"authorization": "Bearer secret"},
        )
        assert resp.status_code == 200

    def test_sse_requires_token(self):
        client = self._client(token="secret")
        resp = client.get("/sse")
        assert resp.status_code == 401


class TestMCPStreamableAuth:
    def _client(self, token=None):
        from starlette.testclient import TestClient

        if token is not None:
            ConfigCenter.get_instance().set("mcp.auth_token", token)
        registry = MCPToolRegistry()
        registry.register_tools()
        app = create_streamable_app(registry)
        return TestClient(app)

    def test_initialize_requires_token(self):
        client = self._client(token="tok")
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 401

    def test_delete_requires_token(self):
        client = self._client(token="tok")
        resp = client.delete("/mcp", headers={"mcp-session-id": "mcp-x"})
        assert resp.status_code == 401

    def test_get_requires_token(self):
        client = self._client(token="tok")
        resp = client.get("/mcp", headers={"mcp-session-id": "mcp-x"})
        assert resp.status_code == 401

    def test_correct_token_initializes(self):
        client = self._client(token="tok")
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}},
            headers={"accept": "application/json", "authorization": "Bearer tok"},
        )
        assert resp.status_code == 200


# ── HI-3: RemoteControlServer TLS fail-closed ──


class TestRemoteTLSFailClosed:
    def test_bad_cert_raises_not_degrades(self, tmp_path):
        from fusion_cowork.server.remote import RemoteControlServer

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("not a cert")
        key.write_text("not a key")
        server = RemoteControlServer(tls_cert=str(cert), tls_key=str(key))
        with pytest.raises(RuntimeError, match="拒绝降级明文"):  # i18n: user-facing
            server._build_ssl_context()

    def test_no_cert_returns_none(self):
        from fusion_cowork.server.remote import RemoteControlServer

        assert RemoteControlServer()._build_ssl_context() is None


# -- HI-12: CrossDeviceSync inbound TLS fail-closed --


class TestSyncTLSFailClosed:
    def test_bad_cert_raises_not_degrades(self, tmp_path):
        from fusion_cowork.server.sync import CrossDeviceSync

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("not a cert")
        key.write_text("not a key")
        sync = CrossDeviceSync(ssl_cert=str(cert), ssl_key=str(key))
        with pytest.raises(RuntimeError, match="拒绝降级明文"):  # i18n: user-facing
            sync._build_server_ssl_context()

    def test_no_cert_returns_none(self):
        from fusion_cowork.server.sync import CrossDeviceSync

        assert CrossDeviceSync()._build_server_ssl_context() is None


# -- CR-5: DeskRPC identity field stripping + IDOR guard --


class TestDeskAuthStripsIdentity:
    def test_authenticate_strips_identity_fields(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        server = DeskRPCServer()
        params = {
            "space_id": "sp1",
            "operator_id": "attacker_admin",
            "user_id": "attacker_user",
            "author_id": "attacker_author",
            "inviter_id": "attacker_inviter",
            "from_user_id": "attacker_from",
            "_auth_token": "nope",
            "data": "kept",
        }
        authed = server._authenticate(params)
        # Identity fields all stripped, trusted principal = local_user injected
        assert "operator_id" in authed and authed["operator_id"] == "local_user"
        assert authed.get("user_id") == "local_user"
        assert authed.get("author_id") == "local_user"
        assert authed.get("inviter_id") == "local_user"
        assert authed.get("__principal__") == "local_user"
        # Caller-supplied identity + token rejected
        assert authed.get("from_user_id") is None
        assert authed.get("_auth_token") is None
        # Non-identity fields kept
        assert authed.get("data") == "kept"

    def test_authenticate_rejects_bad_token(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        ConfigCenter.get_instance().set("desk.auth_token", "good")
        server = DeskRPCServer()
        # Token is only read in start(); set it directly here
        server._auth_token = "good"
        res = server._authenticate({"_auth_token": "bad", "space_id": "sp1"})
        assert isinstance(res, dict) and "__auth_error__" in res
        assert res["__auth_error__"]["code"] == -32001

    def test_authenticate_accepts_good_token(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        server = DeskRPCServer()
        server._auth_token = "good"
        authed = server._authenticate({"_auth_token": "good", "space_id": "sp1"})
        assert "__auth_error__" not in authed
        assert authed.get("__principal__") == "local_user"


# -- CR-5 IDOR: space access guard (real SpaceStore) --


class TestDeskIDORGuard:
    @pytest.mark.asyncio
    async def test_owner_bypasses(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_owner",
            name="t",
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
        # local_user is the owner -> allowed (even with action=manage_space)
        err = await server._require_space_access("sp_owner", "local_user", "manage_space")
        assert err is None
        await store.close()

    @pytest.mark.asyncio
    async def test_non_member_rejected_idor(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_priv",
            name="t",
            description="",
            owner_id="other_owner",
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
        # local_user is not a member -> IDOR denial
        err = await server._require_space_access("sp_priv", "local_user", "")
        assert err is not None and "无权访问" in err["error"]  # i18n: user-facing
        await store.close()

    @pytest.mark.asyncio
    async def test_member_missing_action_perm_rejected(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.models import (
            Space,
            SpaceConfig,
            SpaceMember,
            SpaceRole,
            SpaceStatus,
        )
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp_member",
            name="t",
            description="",
            owner_id="other_owner",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        # local_user is a VIEWER member
        await store.add_member(
            SpaceMember(
                space_id="sp_member",
                user_id="local_user",
                role=SpaceRole.VIEWER,
                display_name="lu",
                joined_at=now,
                last_active=now,
            )
        )
        server = DeskRPCServer(space_store=store)
        # VIEWER lacks manage_space -> denied
        err = await server._require_space_access("sp_member", "local_user", "manage_space")
        assert err is not None and "权限不足" in err["error"]  # i18n: user-facing
        # VIEWER can view_artifact
        err2 = await server._require_space_access("sp_member", "local_user", "view_artifact")
        assert err2 is None
        await store.close()

    @pytest.mark.asyncio
    async def test_check_space_access_nonexistent_space(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        server = DeskRPCServer(space_store=store)
        err = await server._check_space_access("desk.space.get", {"space_id": "nope", "__principal__": "local_user"})
        assert err is not None and "不存在" in err["error"]  # i18n: user-facing
        await store.close()
