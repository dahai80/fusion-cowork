import asyncio
import json
import stat
from unittest.mock import MagicMock

import pytest

from fusion_cowork.server import shell_registry, web_api
from fusion_cowork.server.shell_registry import (
    _extract_issued_key,
    _mask_key,
    load_module_key,
    save_module_key,
)


@pytest.fixture(autouse=True)
def _key_path(tmp_path, monkeypatch):
    fake = tmp_path / "web_module_key"
    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", fake)
    monkeypatch.setattr(web_api, "_module_key", lambda: load_module_key())
    yield fake


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    payload = payload if payload is not None else {}
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    return resp


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0) if self._responses else _response(200, {})


async def _drive(client, manifest, rounds=0.08):
    task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.01))
    await asyncio.sleep(rounds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestModuleKeyPersistence:
    def test_save_and_load_roundtrip(self):
        assert load_module_key() == ""
        assert save_module_key("fm-fusion-cowork-abc123") is True
        assert load_module_key() == "fm-fusion-cowork-abc123"

    def test_file_mode_0600(self, _key_path):
        save_module_key("fm-x")
        assert stat.S_IMODE(_key_path.stat().st_mode) == 0o600

    def test_empty_key_not_saved(self, _key_path):
        assert save_module_key("") is False
        assert not _key_path.exists()

    def test_mask_key_never_plaintext(self):
        masked = _mask_key("fm-fusion-cowork-abcdef12")
        assert "abcdef12" not in masked
        assert masked == "fm-f****ef12"

    def test_load_missing_file_returns_empty(self):
        assert load_module_key() == ""


class TestIssuedKeyExtraction:
    def test_top_level_fields(self):
        for field in ("api_key", "issued_key", "key", "module_key"):
            assert _extract_issued_key({field: "fm-1"}) == "fm-1"

    def test_nested_data(self):
        assert _extract_issued_key({"data": {"api_key": "fm-2"}}) == "fm-2"

    def test_no_key(self):
        assert _extract_issued_key({"ok": True}) == ""
        assert _extract_issued_key(None) == ""


class TestManifestOmitsApiKey:
    def test_registration_body_omits_api_key_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        manifest = shell_registry.load_manifest()
        assert "api_key" not in manifest

    def test_static_override_kept_when_env_set(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "static-secret")
        manifest = shell_registry.load_manifest()
        assert manifest.get("api_key") == "static-secret"


class TestRegistryLoopIssuesKey:
    async def test_registration_persists_issued_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        manifest = {"id": "fusion-cowork"}
        issued = {"api_key": "fm-fusion-cowork-deadbeef01", "key_id": "k1"}
        client = _FakeClient([_response(200, issued), _response(200, {})])
        await _drive(client, manifest)
        assert load_module_key() == "fm-fusion-cowork-deadbeef01"
        assert client.calls[0]["headers"]["Authorization"] == "Bearer admin-secret"

    async def test_no_key_in_response_nothing_persisted(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient([_response(200, {"ok": True}), _response(200, {})])
        await _drive(client, {"id": "fusion-cowork"})
        assert load_module_key() == ""

    async def test_heartbeat_uses_issued_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        save_module_key("fm-issued-heartbeat")
        client = _FakeClient([_response(200, {}), _response(200, {}), _response(200, {})])
        await _drive(client, {"id": "fusion-cowork"})
        auths = [c["headers"]["Authorization"] for c in client.calls]
        assert auths[0] == "Bearer admin-secret"
        assert "Bearer fm-issued-heartbeat" in auths

    async def test_heartbeat_401_with_issued_key_reregisters(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        save_module_key("fm-stale")
        client = _FakeClient([_response(200, {}), _response(401), _response(200, {}), _response(200, {})])
        await _drive(client, {"id": "fusion-cowork"})
        urls = [c["url"] for c in client.calls]
        assert "/heartbeat" in urls[1]
        assert urls[2].endswith("/shell/manifests")  # 401 后重注册
        assert client.calls[2]["headers"]["Authorization"] == "Bearer admin-secret"


class TestInboundAuthDualAccept:
    def test_check_static_key_accepts_issued_key(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        save_module_key("fm-inbound")
        assert web_api.auth_configured() is True
        result = web_api.check_static_key("fm-inbound")
        assert result is not None
        assert result["key_id"] == "module-issued"
        assert web_api.check_static_key("wrong") is None

    def test_check_static_key_accepts_env_key_during_grace(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "env-static")
        save_module_key("fm-new")
        assert web_api.check_static_key("env-static")["key_id"] == "static-env"
        assert web_api.check_static_key("fm-new")["key_id"] == "module-issued"

    def test_no_keys_configured_open(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        assert web_api.auth_configured() is False
        assert web_api.check_static_key("anything") is None
