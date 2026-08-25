"""Stage 2 — JWT 认证 + secret 脱敏 + principal 从 claim 测试 (v0.4.0)。

覆盖:
- JWTVerifier HS256 有效/过期/篡改/错 secret/错租户 claim
- JWTVerifier RS256 (本地公钥) 有效
- verify_static_token: 配/未配 expected, require_jwt 模式
- verify_any_token: JWT 优先 + 静态 fallback
- desk_rpc _authenticate: 注入 __tenant_id__/__principal__ 从 JWT claim
- config_center: secret key 日志脱敏 (grep 无明文 token)
- cli: ntfy_token 打印脱敏 (grep 无明文 token)
"""

from __future__ import annotations

import logging
import time

import pytest

jwt_py = pytest.importorskip("jwt")

from fusion_cowork.auth import (
    JWTVerifier,
    get_default_verifier,
    require_jwt,
    verify_any_token,
    verify_static_token,
)
from fusion_cowork.auth import jwt as jwt_mod
from fusion_cowork.tenant import DEFAULT_TENANT, LOCAL_USER

SECRET = "stage2-test-secret-very-long"


def _make_hs256(payload: dict, secret: str = SECRET, headers: dict | None = None) -> str:
    return jwt_py.encode(payload, secret, algorithm="HS256", headers=headers or {})


def _reset_default_verifier():
    jwt_mod._DEFAULT_VERIFIER = None


@pytest.fixture(autouse=True)
def _clean_verifier():
    _reset_default_verifier()
    yield
    _reset_default_verifier()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "FUSION_JWT_SECRET",
        "FUSION_JWKS_URL",
        "FUSION_JWT_PUBLIC_KEY",
        "FUSION_JWT_ISSUER",
        "FUSION_JWT_AUDIENCE",
        "FUSION_JWT_LEEWAY",
        "FUSION_REQUIRE_JWT",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ── JWTVerifier HS256 ──


class TestHS256:
    def test_valid_token_extracts_claims(self):
        v = JWTVerifier(secret=SECRET)
        assert v.active
        token = _make_hs256({"tenant_id": "acme", "user_id": "alice", "sub": "alice"})
        p = v.verify_token(token)
        assert p is not None
        assert p.tenant_id == "acme"
        assert p.user_id == "alice"

    def test_claim_fallback_keys(self):
        v = JWTVerifier(secret=SECRET)
        # tid + sub 变体
        token = _make_hs256({"tid": "beta", "sub": "bob"})
        p = v.verify_token(token)
        assert p.tenant_id == "beta"
        assert p.user_id == "bob"
        token2 = _make_hs256({"tenant": "gamma", "uid": "carol"})
        p2 = v.verify_token(token2)
        assert p2.tenant_id == "gamma"
        assert p2.user_id == "carol"

    def test_missing_tenant_defaults(self):
        v = JWTVerifier(secret=SECRET)
        token = _make_hs256({"sub": "dave"})
        p = v.verify_token(token)
        assert p.tenant_id == DEFAULT_TENANT
        assert p.user_id == "dave"

    def test_expired_token_rejected(self):
        v = JWTVerifier(secret=SECRET)
        token = _make_hs256({"tenant_id": "acme", "user_id": "alice", "exp": int(time.time()) - 3600})
        assert v.verify_token(token) is None

    def test_tampered_token_rejected(self):
        v = JWTVerifier(secret=SECRET)
        token = _make_hs256({"tenant_id": "acme", "user_id": "alice"})
        tampered = token[:-4] + "AAAA"
        assert v.verify_token(tampered) is None

    def test_wrong_secret_rejected(self):
        v = JWTVerifier(secret=SECRET)
        token = _make_hs256({"tenant_id": "acme"}, secret="other-secret-also-long")
        assert v.verify_token(token) is None

    def test_empty_token_rejected(self):
        v = JWTVerifier(secret=SECRET)
        assert v.verify_token("") is None
        assert v.verify_token(None) is None  # type: ignore[arg-type]

    def test_inactive_verifier_returns_none(self):
        v = JWTVerifier()
        assert not v.active
        assert v.verify_token("anything") is None

    def test_issuer_check(self):
        v = JWTVerifier(secret=SECRET, issuer="fusion")
        token = _make_hs256({"tenant_id": "acme", "iss": "fusion"})
        assert v.verify_token(token) is not None
        bad = _make_hs256({"tenant_id": "acme", "iss": "other"})
        assert v.verify_token(bad) is None


# ── JWTVerifier RS256 (本地公钥, 不走 JWKS 远程) ──


class TestRS256:
    def test_rs256_local_public_key(self):
        pytest.importorskip("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = jwt_py.encode(
            {"tenant_id": "rs256-tenant", "user_id": "rsa-user"},
            priv_pem,
            algorithm="RS256",
        )
        v = JWTVerifier(public_key=pub_pem.decode("utf-8"))
        assert v.active
        p = v.verify_token(token)
        assert p is not None
        assert p.tenant_id == "rs256-tenant"
        assert p.user_id == "rsa-user"

    def test_rs256_wrong_public_key_rejected(self):
        pytest.importorskip("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pub_pem = other.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = jwt_py.encode({"tenant_id": "x"}, priv_pem, algorithm="RS256")
        v = JWTVerifier(public_key=other_pub_pem.decode("utf-8"))
        assert v.verify_token(token) is None


# ── 静态 token fallback ──


class TestStaticFallback:
    def test_no_expected_no_token_local(self):
        # 本地无认证 → 默认 principal (向后兼容)
        p = verify_static_token(None, None)
        assert p is not None
        assert p.is_local

    def test_expected_match(self):
        p = verify_static_token("my-static-token", "my-static-token")
        assert p is not None
        assert p.is_local

    def test_expected_mismatch(self):
        assert verify_static_token("wrong", "my-static-token") is None

    def test_expected_missing_token(self):
        assert verify_static_token("", "my-static-token") is None
        assert verify_static_token(None, "my-static-token") is None

    def test_require_jwt_blocks_local_fallback(self, monkeypatch):
        monkeypatch.setenv("FUSION_REQUIRE_JWT", "1")
        assert require_jwt() is True
        # 生产模式 + 无静态 token → 拒绝降级
        assert verify_static_token(None, None) is None
        # 生产模式 + 有静态 token → 仍可校验
        assert verify_static_token("ok", "ok") is not None


# ── verify_any_token (WS/sync/remote 共用) ──


class TestVerifyAnyToken:
    def test_jwt_preferred_over_static(self, monkeypatch):
        monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
        _reset_default_verifier()
        token = _make_hs256({"tenant_id": "acme", "user_id": "alice"})
        p = verify_any_token(token, "some-static")
        assert p is not None
        assert p.tenant_id == "acme"
        assert p.user_id == "alice"

    def test_static_fallback_when_no_jwt(self):
        # 无 JWT env → 走静态
        p = verify_any_token("my-static", "my-static")
        assert p is not None
        assert p.is_local

    def test_jwt_invalid_falls_to_static(self, monkeypatch):
        monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
        _reset_default_verifier()
        # 不是有效 JWT → 静态 fallback
        p = verify_any_token("my-static", "my-static")
        assert p is not None
        assert p.is_local

    def test_both_fail_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
        _reset_default_verifier()
        # require_jwt 模式 + 无静态 + 非 JWT
        monkeypatch.setenv("FUSION_REQUIRE_JWT", "1")
        assert verify_any_token("garbage", None) is None


# ── desk_rpc _authenticate 注入 tenant_id ──


class TestDeskRpcAuthInject:
    def test_jwt_injects_tenant_and_user(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
        _reset_default_verifier()
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer(sock_path=str(tmp_path / "s.sock"))
        srv._jwt_verifier = get_default_verifier()
        token = _make_hs256({"tenant_id": "tenant-z", "user_id": "zoe"})
        authed = srv._authenticate({"_auth_token": token, "user_id": "attacker", "operator_id": "attacker"})
        assert "__auth_error__" not in authed
        assert authed["__tenant_id__"] == "tenant-z"
        assert authed["__principal__"] == "zoe"
        # 身份字段不信 params (CR-5 反 IDOR): attacker 注入被覆盖
        assert authed["user_id"] == "zoe"
        assert authed["operator_id"] == "zoe"

    def test_jwt_expired_rejected_in_require_mode(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
        monkeypatch.setenv("FUSION_REQUIRE_JWT", "1")
        _reset_default_verifier()
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer(sock_path=str(tmp_path / "s.sock"))
        srv._jwt_verifier = get_default_verifier()
        token = _make_hs256({"tenant_id": "t", "user_id": "u", "exp": int(time.time()) - 3600})
        authed = srv._authenticate({"_auth_token": token})
        assert "__auth_error__" in authed
        assert authed["__auth_error__"]["code"] == -32001

    def test_no_auth_local_fallback(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer(sock_path=str(tmp_path / "s.sock"))
        srv._jwt_verifier = None
        authed = srv._authenticate({"user_id": "anyone"})
        assert authed["__tenant_id__"] == DEFAULT_TENANT
        assert authed["__principal__"] == LOCAL_USER or authed["__principal__"] == "local_user"

    def test_static_token_fallback(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer(sock_path=str(tmp_path / "s.sock"))
        srv._jwt_verifier = None
        srv._auth_token = "desk-static"
        authed = srv._authenticate({"_auth_token": "desk-static"})
        assert "__auth_error__" not in authed
        assert authed["__tenant_id__"] == DEFAULT_TENANT

    def test_static_token_mismatch_rejected(self, tmp_path):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer(sock_path=str(tmp_path / "s.sock"))
        srv._jwt_verifier = None
        srv._auth_token = "desk-static"
        authed = srv._authenticate({"_auth_token": "wrong"})
        assert "__auth_error__" in authed


# ── secret 脱敏 ──


class TestSecretRedaction:
    def test_config_center_set_log_redacts_secret(self, caplog, tmp_path):
        from fusion_cowork.config_center import ConfigCenter

        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        secret_val = "super-secret-token-12345"
        with caplog.at_level(logging.INFO, logger="fusion_cowork.config_center"):
            cc.set("ai.api_token", secret_val)
        full = "\n".join(r.getMessage() for r in caplog.records)
        assert "super-secret-token-12345" not in full
        assert "[REDACTED]" in full

    def test_config_center_to_dict_redacts_secret(self, tmp_path):
        from fusion_cowork.config_center import ConfigCenter

        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        cc.set("ai.api_key", "plaintext-key-value")
        d = cc.to_dict()
        entries = d.get("entries", {})
        assert entries.get("ai.api_key", {}).get("value") == "[REDACTED]"

    def test_config_center_non_secret_not_redacted(self, tmp_path):
        from fusion_cowork.config_center import ConfigCenter

        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        cc.set("ai.base_url", "http://localhost:11432")
        d = cc.to_dict()
        assert d["entries"]["ai.base_url"]["value"] == "http://localhost:11432"

    def test_cli_ntfy_token_display_redacted(self, tmp_path, monkeypatch):
        import fusion_cowork.cli as cli_mod

        captured = []

        class FakeConsole:
            def print_info(self, msg):
                captured.append(str(msg))

            def print_result(self, msg):
                captured.append(str(msg))

            def print_success(self, msg):
                captured.append(str(msg))

            def print_error(self, msg):
                captured.append(str(msg))

        monkeypatch.setattr(cli_mod, "console", FakeConsole())
        from fusion_cowork.config_center import ConfigCenter

        # push_config 用 ConfigCenter.get_instance() 单例 — 设单例值
        ConfigCenter._instance = None  # 清旧单例
        cc = ConfigCenter(config_file=str(tmp_path / "config.json"))
        ConfigCenter._instance = cc
        cc.set("push.bark_url", "https://bark.example.com")
        cc.set("push.ntfy_url", "https://ntfy.example.com")
        secret = "very-long-ntfy-token-12345678"
        cc.set("push.ntfy_token", secret)
        try:
            cli_mod.push_config.callback(  # type: ignore[attr-defined]
                bark_url=None,
                ntfy_url=None,
                ntfy_token=None,
                sound=None,
                priority=None,
            )
        finally:
            ConfigCenter._instance = None
        joined = "\n".join(captured)
        assert "very-long-ntfy-token-12345678" not in joined
        assert "REDACTED" in joined or "***" in joined or "已设置" in joined
