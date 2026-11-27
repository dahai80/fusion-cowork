"""issue #177: shell heartbeat carries the manifest — module-side adoption of gateway #284 refresh contract.

Contract:
- The heartbeat POST body carries the manifest (including id); the gateway converges ext fields via HeartbeatWithManifest.
- The on-disk manifest is re-read every beat — disk edits (adding/removing pages) converge within one heartbeat interval, no restart needed.
- If re-reading fails (corrupt/missing file), the registration-time copy is used and the heartbeat continues.
- fm- key auth path unchanged (401 -> re-register)."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from fusion_cowork.server import shell_registry
from fusion_cowork.server.shell_registry import load_module_key


@pytest.fixture(autouse=True)
def _key_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key")
    yield


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    payload = payload if payload is not None else {}
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    return resp


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _response(200, {})


def _write_manifest(path, pages):
    path.write_text(
        json.dumps({"id": "fusion-cowork", "upstream": "http://127.0.0.1:11461", "pages": pages}, ensure_ascii=False),
        encoding="utf-8",
    )


async def _drive(client, manifest, seconds=0.25):
    task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.02))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _heartbeats(calls):
    return [c for c in calls if "/heartbeat" in c["url"]]


@pytest.fixture
def manifest_env(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(path))
    _write_manifest(path, [{"route": "/workflows"}])
    return path


class TestHeartbeatCarriesManifest:
    async def test_heartbeat_body_is_manifest(self, manifest_env):
        client = _FakeClient()
        manifest = shell_registry.load_manifest()
        await _drive(client, manifest)
        beats = _heartbeats(client.calls)
        assert beats, "注册成功后应有心跳"  # i18n: user-facing
        for beat in beats:
            assert beat["json"]["id"] == "fusion-cowork"
            assert beat["json"]["upstream"] == "http://127.0.0.1:11461"

    async def test_disk_edit_converges_within_one_beat(self, manifest_env):
        client = _FakeClient()
        manifest = shell_registry.load_manifest()
        drive_task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.02))
        await asyncio.sleep(0.1)
        # Add a page on disk without restarting — the next beat's heartbeat body should carry the new pages
        _write_manifest(manifest_env, [{"route": "/workflows"}, {"route": "/probe"}])
        await asyncio.sleep(0.15)
        drive_task.cancel()
        try:
            await drive_task
        except asyncio.CancelledError:
            pass
        beats = _heartbeats(client.calls)
        routes = [{p["route"] for p in b["json"]["pages"]} for b in beats]
        assert routes[0] == {"/workflows"}, "首拍携带旧 manifest"  # i18n: user-facing
        assert any("/probe" in r for r in routes), "磁盘编辑后应在一个心跳周期内收敛"  # i18n: user-facing

    async def test_reread_failure_falls_back_to_registration_copy(self, manifest_env):
        client = _FakeClient()
        manifest = shell_registry.load_manifest()
        drive_task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.02))
        await asyncio.sleep(0.1)
        manifest_env.unlink()  # on-disk manifest disappears
        await asyncio.sleep(0.1)
        drive_task.cancel()
        try:
            await drive_task
        except asyncio.CancelledError:
            pass
        beats = _heartbeats(client.calls)
        assert beats, "manifest 重读失败不应中断心跳"  # i18n: user-facing
        late = beats[-1]["json"]
        assert late["id"] == "fusion-cowork"
        assert {p["route"] for p in late["pages"]} == {"/workflows"}, "回退到注册时版本"  # i18n: user-facing

    async def test_heartbeat_401_with_issued_key_reregisters(self, manifest_env):
        class _UnauthorizedClient:
            def __init__(self):
                self.calls = []
                self._beats = 0

            async def post(self, url, json=None, headers=None):
                self.calls.append({"url": url, "json": json, "headers": headers})
                if "/heartbeat" in url:
                    self._beats += 1
                    if self._beats == 1:
                        return _response(401)
                return _response(200, {})

        load_module_key()  # ensure path init
        from fusion_cowork.server.shell_registry import save_module_key

        save_module_key("fm-stale")
        client = _UnauthorizedClient()
        manifest = shell_registry.load_manifest()
        await _drive(client, manifest, seconds=0.15)
        urls = [c["url"] for c in client.calls]
        assert "/heartbeat" in urls[1]
        assert urls[2].endswith("/shell/manifests"), "401 后重注册"  # i18n: user-facing
        assert client.calls[2]["headers"]["Authorization"] == "Bearer admin-secret"
        # Heartbeat bodies after re-registration still carry the manifest
        later_beats = _heartbeats(client.calls)[1:]
        for beat in later_beats:
            assert beat["json"]["id"] == "fusion-cowork"
