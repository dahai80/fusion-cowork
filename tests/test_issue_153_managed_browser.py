"""issue #153: 托管浏览器 — 独立 profile 启动 / 自动 CDP 端口 / cookie 导入 / 节点与 API。"""

from __future__ import annotations

import asyncio
import os

import pytest

import fusion_cowork.nodes
from fusion_cowork.engine.node import NodeRegistry


@pytest.fixture(autouse=True)
def _ensure_nodes_loaded():
    # 跨测试文件 NodeRegistry 可能被 clear(), 每测试前确保内置节点在册
    if "managed_browser" not in NodeRegistry._registry:
        fusion_cowork.nodes.import_all_nodes()
    yield


class _FakeProc:
    instances = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False
        _FakeProc.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_launch(monkeypatch, tmp_path):
    from fusion_cowork import managed_browser as mb

    _FakeProc.instances.clear()
    monkeypatch.setattr(mb, "find_chromium_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(mb.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(mb.ManagedBrowser, "_wait_ready", _async_noop)
    monkeypatch.delenv("FUSION_BROWSER_CDP", raising=False)
    browser = mb.ManagedBrowser(profile_dir=tmp_path / "profile")
    yield browser
    os.environ.pop("FUSION_BROWSER_CDP", None)


async def _async_noop(self, port):
    return None


def test_parse_netscape_cookie_file(tmp_path):
    from fusion_cowork.managed_browser import parse_cookie_file

    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t1700000000\tsession\tabc123\nbad-line\n",
        encoding="utf-8",
    )
    cookies = parse_cookie_file(path)
    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie["domain"] == ".example.com"
    assert cookie["name"] == "session"
    assert cookie["secure"] is True
    assert cookie["expires"] == 1700000000.0


def test_find_binary_env_override(monkeypatch, tmp_path):
    from fusion_cowork import managed_browser as mb

    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("FUSION_COWORK_CHROMIUM", str(fake))
    assert mb.find_chromium_binary() == str(fake)

    monkeypatch.setenv("FUSION_COWORK_CHROMIUM", str(tmp_path / "missing"))
    with pytest.raises(mb.ManagedBrowserError):
        mb.find_chromium_binary()


def test_launch_uses_independent_profile_and_cdp_port(fake_launch):
    info = asyncio.run(fake_launch.launch(url="https://example.com", port=9333, headless=True))
    assert info["running"] is True
    assert info["port"] == 9333
    proc = _FakeProc.instances[-1]
    joined = " ".join(proc.args)
    assert "--remote-debugging-port=9333" in joined
    assert "--user-data-dir=" in joined
    assert "--headless=new" in joined
    assert joined.endswith("https://example.com")
    assert os.environ["FUSION_BROWSER_CDP"] == "9333"
    assert fake_launch.status()["running"] is True


def test_launch_rolls_back_when_cdp_never_ready(monkeypatch, tmp_path):
    from fusion_cowork import managed_browser as mb

    _FakeProc.instances.clear()
    monkeypatch.setattr(mb, "find_chromium_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(mb.subprocess, "Popen", _FakeProc)

    async def _timeout(self, port):
        raise mb.ManagedBrowserError("not ready")

    monkeypatch.setattr(mb.ManagedBrowser, "_wait_ready", _timeout)
    browser = mb.ManagedBrowser(profile_dir=tmp_path / "p")
    with pytest.raises(mb.ManagedBrowserError):
        asyncio.run(browser.launch(port=9444))
    assert _FakeProc.instances[-1].terminated is True
    assert browser.status()["running"] is False


def test_close_terminates_process(fake_launch):
    asyncio.run(fake_launch.launch(port=9555))
    assert asyncio.run(fake_launch.close()) is True
    assert _FakeProc.instances[-1].terminated is True
    assert "FUSION_BROWSER_CDP" not in os.environ
    assert fake_launch.status()["running"] is False


def test_node_reports_clear_error_without_browser(monkeypatch):
    from fusion_cowork import managed_browser as mb
    from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
    from fusion_cowork.nodes import import_all_nodes

    import_all_nodes()
    mb.reset_managed_browser()
    monkeypatch.setenv("FUSION_COWORK_CHROMIUM", "/definitely/missing/chrome")
    node = NodeRegistry.create("managed_browser", config=NodeConfig(params={"action": "launch"}))
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.FAILED
    assert "不存在" in result.error
    mb.reset_managed_browser()


def test_node_status_action(monkeypatch):
    from fusion_cowork import managed_browser as mb
    from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
    from fusion_cowork.nodes import import_all_nodes

    import_all_nodes()
    mb.reset_managed_browser()
    node = NodeRegistry.create("managed_browser", config=NodeConfig(params={"action": "status"}))
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS
    assert result.data["running"] is False


class TestBrowserApi:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "fc-test-153")
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            engine=WorkflowEngine(),
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
        )
        return TestClient(app, headers={"x-api-key": "fc-test-153"})

    def test_status_not_running(self, client, monkeypatch):
        from fusion_cowork import managed_browser as mb

        mb.reset_managed_browser()
        resp = client.get("/api/browser/status")
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    def test_screenshot_409_when_not_running(self, client, monkeypatch):
        from fusion_cowork import managed_browser as mb

        mb.reset_managed_browser()
        resp = client.get("/api/browser/screenshot")
        assert resp.status_code == 409

    def test_launch_reports_503_without_binary(self, client, monkeypatch):
        from fusion_cowork import managed_browser as mb

        mb.reset_managed_browser()
        monkeypatch.setenv("FUSION_COWORK_CHROMIUM", "/definitely/missing/chrome")
        resp = client.post("/api/browser/launch", json={"url": "https://example.com"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "browser_unavailable"
        mb.reset_managed_browser()
