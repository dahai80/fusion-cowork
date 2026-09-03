"""issue #88 — fusion-identity 集成测试 (offline, httpx MockTransport)。

无 live fusion-identity (CI 无 11470)。全部 sync httpx.Client 经 MockTransport 模拟。
opt-in OFF (无 FUSION_IDENTITY_ENABLED) → 零行为变化, 全套件绿。
"""

from __future__ import annotations

import time

import httpx
import pytest

import fusion_cowork.auth.identity as identity_mod
from fusion_cowork.auth.identity import (
    IdentityClient,
    IdentityVerifyResult,
    is_identity_enabled,
    make_verify_jwt_callback,
)


def _make_token(tid="t1", jti="j-1", secret="secret"):
    import jwt as pyjwt

    return pyjwt.encode(
        {"tid": tid, "sub": "u1", "role": "admin", "jti": jti, "scope": ["a", "b"], "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _client_with_transport(handler, **kw):
    c = IdentityClient(base_url="http://127.0.0.1:11470", service_token="svc-tok", **kw)
    c._client = httpx.Client(transport=_mock_transport(handler), timeout=2.0)
    return c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_IDENTITY_URL", raising=False)
    monkeypatch.delenv("FUSION_IDENTITY_SERVICE_TOKEN", raising=False)
    identity_mod.reset_identity_client()
    yield
    identity_mod.reset_identity_client()


class TestOptInOff:
    def test_disabled_by_default(self):
        assert is_identity_enabled() is False

    def test_get_identity_client_none_when_disabled(self):
        assert identity_mod.get_identity_client() is None

    def test_enabled_no_service_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.delenv("FUSION_IDENTITY_SERVICE_TOKEN", raising=False)
        assert identity_mod.get_identity_client() is None

    def test_enabled_with_token_returns_client(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        c = identity_mod.get_identity_client()
        assert c is not None
        assert isinstance(c, IdentityClient)


class TestIdentityClientWire:
    def test_verify_success(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.path == "/api/v1/auth/verify"
            assert request.headers["authorization"] == "Bearer svc-tok"
            import json as _json

            body = _json.loads(request.content)
            assert "token" in body
            return httpx.Response(200, json={"tid": "t1", "role": "admin", "scopes": ["a"], "quota": {"max_spaces": 5}})

        c = _client_with_transport(handler)
        result = c.verify(_make_token())
        assert result is not None
        assert result.tid == "t1"
        assert result.role == "admin"
        assert result.scopes == ("a",)
        assert result.quota == {"max_spaces": 5}
        assert result.revoked is False
        assert len(calls) == 1

    def test_verify_cache_hit_skips_http(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"tid": "t1", "role": "r"})

        c = _client_with_transport(handler, cache_ttl=60)
        tok = _make_token(jti="j-cache")
        r1 = c.verify(tok)
        r2 = c.verify(tok)
        assert r1 is not None and r2 is not None
        assert r1.tid == r2.tid == "t1"
        assert len(calls) == 1, "第二次应命中缓存不发起 HTTP"

    def test_verify_revoked_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tid": "t1", "revoked": True})

        c = _client_with_transport(handler)
        assert c.verify(_make_token()) is None

    def test_verify_tenant_inactive_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tid": "t1", "tenant_status": "suspended"})

        c = _client_with_transport(handler)
        assert c.verify(_make_token()) is None

    def test_verify_non200_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad token"})

        c = _client_with_transport(handler)
        assert c.verify(_make_token()) is None

    def test_verify_conn_fail_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no identity")

        c = _client_with_transport(handler)
        assert c.verify(_make_token()) is None

    def test_verify_empty_token(self):
        c = _client_with_transport(lambda r: httpx.Response(200, json={}))
        assert c.verify("") is None
        assert c.verify(None) is None  # type: ignore[arg-type]

    def test_emit_usage_best_effort(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(202)

        c = _client_with_transport(handler)
        assert c.emit_usage("t1", "messages", 3, user_id="u1") is True
        assert len(calls) == 1
        assert calls[0].url.path == "/api/v1/tenants/t1/usage"

    def test_emit_usage_fail_returns_false(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        c = _client_with_transport(handler)
        assert c.emit_usage("t1", "messages", 1) is False

    def test_ping(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        c = _client_with_transport(handler)
        assert c.ping() is True


class TestVerifyJwtCallback:
    def test_callback_returns_claims_dict(self):
        c = _client_with_transport(lambda r: httpx.Response(200, json={"tid": "t9", "role": "owner", "scopes": ["x"]}))
        cb = make_verify_jwt_callback(c)
        claims = cb(_make_token(tid="t9"))
        assert claims["tid"] == "t9"
        assert claims["role"] == "owner"
        assert "scopes" in claims

    def test_callback_raises_on_revoked(self):
        c = _client_with_transport(lambda r: httpx.Response(200, json={"tid": "t9", "revoked": True}))
        cb = make_verify_jwt_callback(c)
        with pytest.raises(identity_mod.IdentityError):
            cb(_make_token())

    def test_callback_raises_on_fail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        c = _client_with_transport(handler)
        cb = make_verify_jwt_callback(c)
        with pytest.raises(identity_mod.IdentityError):
            cb(_make_token())


class TestFallbackVerifyAnyToken:
    def test_identity_enabled_delegates(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(
            transport=_mock_transport(lambda r: httpx.Response(200, json={"tid": "tid-99", "role": "r"})),
            timeout=2.0,
        )
        # reset jwt verifier singleton to pick up identity adapter
        import fusion_cowork.auth.jwt as jwt_mod
        from fusion_cowork.auth.fallback import verify_any_token

        jwt_mod._DEFAULT_VERIFIER = None
        try:
            principal = verify_any_token(_make_token(tid="tid-99"), None)
            assert principal is not None
            assert principal.tenant_id == "tid-99"
        finally:
            jwt_mod._DEFAULT_VERIFIER = None

    def test_identity_enabled_revoked_no_static_fallback(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        monkeypatch.setenv("FUSION_REQUIRE_JWT", "1")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(
            transport=_mock_transport(lambda r: httpx.Response(200, json={"tid": "t1", "revoked": True})),
            timeout=2.0,
        )
        import fusion_cowork.auth.jwt as jwt_mod
        from fusion_cowork.auth.fallback import verify_any_token

        jwt_mod._DEFAULT_VERIFIER = None
        try:
            assert verify_any_token(_make_token(), "static-expected") is None
        finally:
            jwt_mod._DEFAULT_VERIFIER = None
            monkeypatch.delenv("FUSION_REQUIRE_JWT", raising=False)


class TestQuotasFromIdentity:
    def test_load_from_identity_cache(self):
        class FakeClient:
            def __init__(self):
                result = IdentityVerifyResult(tid="t1", quota={"max_spaces": 3, "max_messages_per_space": 10})
                self._cache = {"j": (0.0, result)}

            def emit_usage(self, *a, **k):
                return True

        from fusion_cowork.security.quotas import QuotaEnforcer

        enforcer = QuotaEnforcer(identity_client=FakeClient())
        q = enforcer._load_quotas("t1")
        assert q.max_spaces == 3
        assert q.max_messages_per_space == 10
        assert q.max_artifacts_per_space == -1

    def test_record_usage_calls_emit(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def emit_usage(self, tenant_id, metric, value, source="fusion-cowork", model=None, user_id=None):
                self.calls.append((tenant_id, metric, value))
                return True

        from fusion_cowork.security.quotas import QuotaEnforcer

        fake = FakeClient()
        enforcer = QuotaEnforcer(identity_client=fake)
        enforcer.record_usage("t1", "messages", 2)
        assert fake.calls == [("t1", "messages", 2)]

    def test_no_identity_client_current_path(self):
        from fusion_cowork.security.quotas import QuotaEnforcer

        enforcer = QuotaEnforcer()
        q = enforcer._load_quotas("t1")
        assert q.unlimited is True


class TestDeskRpcAuthenticate:
    def test_identity_enabled_verifies(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(
            transport=_mock_transport(lambda r: httpx.Response(200, json={"tid": "t-desk", "role": "r"})),
            timeout=2.0,
        )
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer.__new__(DeskRPCServer)
        srv._jwt_verifier = None
        srv._principal_resolver = None
        srv._auth_token = None
        authed = srv._authenticate({"_auth_token": _make_token(tid="t-desk"), "x-user-id": "u-desk"})
        assert "__auth_error__" not in authed
        assert authed["__tenant_id__"] == "t-desk"
        assert authed["__principal__"] == "u-desk"

    def test_identity_revoked_fail_closed(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(
            transport=_mock_transport(lambda r: httpx.Response(200, json={"tid": "t1", "revoked": True})),
            timeout=2.0,
        )
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer.__new__(DeskRPCServer)
        srv._jwt_verifier = None
        srv._principal_resolver = None
        srv._auth_token = None
        authed = srv._authenticate({"_auth_token": _make_token()})
        assert "__auth_error__" in authed

    def test_disabled_uses_static(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer.__new__(DeskRPCServer)
        srv._jwt_verifier = None
        srv._principal_resolver = None
        srv._auth_token = "static-tok"
        authed = srv._authenticate({"_auth_token": "static-tok"})
        assert "__auth_error__" not in authed
        assert authed["__tenant_id__"] == "default"


class TestSpaceApiMiddleware:
    def _build_app(self, monkeypatch, verify_resp):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(
            transport=_mock_transport(lambda r: httpx.Response(200, json=verify_resp)),
            timeout=2.0,
        )

        from fusion_cowork.space.api import create_space_api

        class Svc:
            async def create(self, **kw):
                class S:
                    def to_dict(self):
                        return {"id": "s1", "name": kw.get("name", ""), "owner_id": kw.get("owner_id", "")}

                return S()

        app = create_space_api(Svc(), member_svc=type("M", (), {"_store": None})(), chat_svc=None, kb_svc=None)
        return app

    @pytest.mark.asyncio
    async def test_missing_tenant_header_401(self, monkeypatch):
        app = self._build_app(monkeypatch, {"tid": "t1", "role": "r"})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            resp = await client.post("/spaces", json={"name": "n"}, headers={"Authorization": f"Bearer {_make_token()}"})
            assert resp.status_code == 401
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_tenant_mismatch_401(self, monkeypatch):
        app = self._build_app(monkeypatch, {"tid": "t1", "role": "r"})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            resp = await client.post(
                "/spaces",
                json={"name": "n"},
                headers={"Authorization": f"Bearer {_make_token()}", "X-Tenant-Id": "other"},
            )
            assert resp.status_code == 401
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_valid_token_201_and_bridge(self, monkeypatch):
        app = self._build_app(monkeypatch, {"tid": "t1", "role": "r"})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            resp = await client.post(
                "/spaces",
                json={"name": "n"},
                headers={"Authorization": f"Bearer {_make_token()}", "X-Tenant-Id": "t1", "X-User-Id": "u1"},
            )
            assert resp.status_code == 201, resp.text
        finally:
            await client.aclose()


class TestMcpHttpMiddleware:
    @pytest.mark.asyncio
    async def test_streamable_initialize_no_token_allowed(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        identity_mod.reset_identity_client()
        c = identity_mod.get_identity_client()
        c._client = httpx.Client(transport=_mock_transport(lambda r: httpx.Response(200, json={"tid": "t1"})), timeout=2.0)
        from fusion_cowork.server.mcp_http import create_streamable_app
        from fusion_cowork.server.mcp_server import MCPToolRegistry

        reg = MCPToolRegistry()
        app = create_streamable_app(reg)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            # initialize 无 token → require_jwt=False 放行 (exempt /mcp)
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert resp.status_code in (200, 406), resp.text
        finally:
            await client.aclose()
