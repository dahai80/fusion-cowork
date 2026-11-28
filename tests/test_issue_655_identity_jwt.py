"""issue #655: fusion-identity RS256 service tokens on the web_api plane.

The web middleware falls back to JWT verification when the static-key check
misses and the bearer looks like a JWT (three segments). Verified via
fusion_cowork.auth.jwt (JWKS/RS256 or identity adapter):
- role="service" (identity service account) → run lifecycle scope
  (/api/runs + /api/delivery/invites + /api/permissions HITL approvals)
- other roles → admin
- invalid/expired tokens stay 401 (fail-closed)
- static keys keep working (transition dual-active)
- JWT-only deployment (no static keys) counts as auth configured
"""

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from fusion_cowork.server import web_api

_STATIC_KEY = "static-user-key-655"

_PROBE_WF = {
    "name": "jwt-probe-655",
    "nodes": [{"id": "n1", "name": "file_find", "params": {"patterns": ["*.txt"], "search_path": "/tmp"}}],
    "edges": [],
}


@pytest.fixture(scope="module")
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key()
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True)
    jwk["kid"] = "test-kid-655"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return key, {"keys": [jwk]}


def _make_token(rsa_key, role="service", sub="fsa-site-bff", expired=False, kid="test-kid-655"):
    import time

    now = int(time.time())
    payload = {
        "sub": sub,
        "tid": "t-655",
        "tenant": "t-655",
        "role": role,
        "scope": ["runs"],
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now - 600 if expired else now + 600,
        "iss": "https://identity.test",
        "aud": "fusion-cowork",
        "jti": "jti-655",
        "type": "access",
    }
    return pyjwt.encode(payload, rsa_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def jwks_env(monkeypatch, rsa_keys):
    _, jwks = rsa_keys
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", _STATIC_KEY)
    monkeypatch.setenv("FUSION_JWKS_URL", "https://jwks.test/keys")
    monkeypatch.setenv("FUSION_JWT_ISSUER", "https://identity.test")
    monkeypatch.setenv("FUSION_JWT_AUDIENCE", "fusion-cowork")
    # reset the lazy verifier cache + module singleton between cases
    _reset_verifiers(monkeypatch)
    yield jwks
    _reset_verifiers(monkeypatch)


@pytest.fixture
def client(monkeypatch, jwks_env, tmp_path):
    import urllib.request
    from unittest.mock import patch

    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")

    def _fake_urlopen(url, timeout=None):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                import json

                return json.dumps(jwks_env).encode()

        return _Resp()

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "workflows")
        yield TestClient(app)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _reset_verifiers(monkeypatch):
    # web_api lazy verifier + the auth.jwt module singleton both cache
    # env-derived state — reset both between cases
    from fusion_cowork.auth import jwt as jwt_mod

    monkeypatch.setattr(web_api, "_JWT_VERIFIER", None, raising=False)
    monkeypatch.setattr(web_api, "_JWT_VERIFIER_TRIED", False, raising=False)
    monkeypatch.setattr(jwt_mod, "_DEFAULT_VERIFIER", None, raising=False)


class TestIdentityServiceToken:
    def test_service_token_runs_allowed(self, client, rsa_keys):
        token = _make_token(rsa_keys[0])
        resp = client.post("/api/runs", headers=_bearer(token), json={"workflow": _PROBE_WF})
        assert resp.status_code == 202

    def test_service_token_approvals_in_scope(self, client, rsa_keys):
        # #655: HITL approvals are run lifecycle for identity service tokens
        token = _make_token(rsa_keys[0])
        assert client.get("/api/permissions/pending", headers=_bearer(token)).status_code == 200

    def test_service_token_workflows_403(self, client, rsa_keys):
        token = _make_token(rsa_keys[0])
        resp = client.get("/api/workflows", headers=_bearer(token))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_service_token_models_403(self, client, rsa_keys):
        token = _make_token(rsa_keys[0])
        assert client.get("/api/models", headers=_bearer(token)).status_code == 403


class TestIdentityUserToken:
    def test_user_token_admin_reach(self, client, rsa_keys):
        token = _make_token(rsa_keys[0], role="admin", sub="admin-655")
        assert client.get("/api/workflows", headers=_bearer(token)).status_code == 200
        resp = client.post("/api/runs", headers=_bearer(token), json={"workflow": _PROBE_WF})
        assert resp.status_code == 202


class TestFailClosed:
    def test_expired_token_401(self, client, rsa_keys):
        token = _make_token(rsa_keys[0], expired=True)
        assert client.get("/api/runs", headers=_bearer(token)).status_code == 401

    def test_garbage_jwt_401(self, client):
        assert client.get("/api/runs", headers=_bearer("a.b.c")).status_code == 401

    def test_missing_key_401(self, client):
        assert client.post("/api/runs", json={"workflow": _PROBE_WF}).status_code == 401

    def test_wrong_role_value_rejected_from_service_scope(self, client, rsa_keys):
        # unknown role → user plane (admin), NOT service scope
        token = _make_token(rsa_keys[0], role="weird", sub="x-655")
        assert client.get("/api/workflows", headers=_bearer(token)).status_code == 200


class TestTransition:
    def test_static_key_still_works(self, client):
        assert client.get("/api/workflows", headers=_bearer(_STATIC_KEY)).status_code == 200

    def test_static_service_key_scope_unchanged(self, client, monkeypatch):
        # #565 scope untouched: static service key still 403 on approvals
        monkeypatch.setenv("FUSION_COWORK_SERVICE_KEY", "svc-static-655")
        assert client.get("/api/permissions/pending", headers=_bearer("svc-static-655")).status_code == 403

    def test_health_open(self, client):
        assert client.get("/health").status_code == 200


class TestConfigured:
    def test_jwt_only_deployment_counts_configured(self, monkeypatch, jwks_env):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        assert web_api.auth_configured() is True

    def test_no_verifier_not_configured(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        monkeypatch.delenv("FUSION_JWKS_URL", raising=False)
        monkeypatch.delenv("FUSION_JWT_SECRET", raising=False)
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        # dev machines hold a persisted gateway-issued module key — stub it
        monkeypatch.setattr(web_api, "_module_key", lambda: "")
        _reset_verifiers(monkeypatch)
        assert web_api.auth_configured() is False
