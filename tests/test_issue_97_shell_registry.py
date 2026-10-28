"""#97: web serve shell 网关注册 — env 门控 / manifest 加载 / 注册-心跳-重注册循环。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("FUSION_SHELL_GATEWAY_URL", "FUSION_ADMIN_KEY", "FUSION_SHELL_MANIFEST"):
        monkeypatch.delenv(var, raising=False)


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"id": "fusion-cowork", "title": "t"}), encoding="utf-8")
    return p


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:22432")
    monkeypatch.setenv("FUSION_ADMIN_KEY", "fg-admin")
    monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(_manifest(tmp_path)))


class TestGating:
    def test_disabled_without_env(self):
        from fusion_cowork.server.shell_registry import shell_registration_enabled

        assert shell_registration_enabled() is False

    def test_enabled_with_both_envs(self, monkeypatch):
        from fusion_cowork.server.shell_registry import shell_registration_enabled

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "fg-admin")
        assert shell_registration_enabled() is True

    def test_enabled_requires_admin_key(self, monkeypatch):
        from fusion_cowork.server.shell_registry import shell_registration_enabled

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        assert shell_registration_enabled() is False


class TestManifestLoad:
    def test_load_default_repo_manifest(self):
        from fusion_cowork.server.shell_registry import load_manifest

        manifest = load_manifest()
        assert manifest["id"] == "fusion-cowork"

    def test_missing_file_raises(self, monkeypatch, tmp_path):
        from fusion_cowork.server.shell_registry import load_manifest

        monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(tmp_path / "missing.json"))
        with pytest.raises(FileNotFoundError):
            load_manifest()

    def test_missing_id_raises(self, monkeypatch, tmp_path):
        from fusion_cowork.server.shell_registry import load_manifest

        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"title": "t"}), encoding="utf-8")
        monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(p))
        with pytest.raises(ValueError):
            load_manifest()


class TestRegistryLoop:
    def test_register_then_heartbeat_then_reregister_on_404(self, monkeypatch, tmp_path):
        from fusion_cowork.server.shell_registry import shell_registry_loop

        _env(monkeypatch, tmp_path)
        calls: list = []
        state = {"heartbeats": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/api/v1/shell/manifests":
                return httpx.Response(200, json={"ok": True})
            state["heartbeats"] += 1
            # heartbeat: first two 200, then 404 → loop must re-register
            return httpx.Response(404 if state["heartbeats"] > 2 else 200)

        async def _go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(shell_registry_loop(client=client, interval=0.01))
                await asyncio.sleep(0.25)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(_go())
        assert calls[0] == "/api/v1/shell/manifests"
        assert "/api/v1/shell/manifests/fusion-cowork/heartbeat" in calls
        # after the 404 the loop must POST the manifest again
        last_register_idx = max(i for i, c in enumerate(calls) if c == "/api/v1/shell/manifests")
        assert last_register_idx > calls.index("/api/v1/shell/manifests/fusion-cowork/heartbeat")

    def test_register_failure_retries_not_fatal(self, monkeypatch, tmp_path):
        from fusion_cowork.server.shell_registry import shell_registry_loop

        _env(monkeypatch, tmp_path)
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(503, text="gateway down")

        async def _go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(shell_registry_loop(client=client, interval=0.01))
                await asyncio.sleep(0.1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(_go())
        assert len(calls) >= 2
        assert all(c == "/api/v1/shell/manifests" for c in calls)

    def test_manifest_missing_disables_loop(self, monkeypatch, tmp_path):
        from fusion_cowork.server.shell_registry import shell_registry_loop

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:22432")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "fg-admin")
        monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(tmp_path / "missing.json"))

        async def _go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
                await asyncio.wait_for(shell_registry_loop(client=client), timeout=2.0)

        # loop returns immediately (logs error, does not spin) — no hang, no raise
        asyncio.run(_go())
