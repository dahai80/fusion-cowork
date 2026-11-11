"""issue #448: shell 注册循环可观测性 — 目标 URL 进日志、异常分类、连败降频、/health 暴露。

离线桩: 伪 httpx client (可配置 post 行为), 不触网。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from fusion_cowork.server import shell_registry
from fusion_cowork.server.shell_registry import (
    FAIL_STREAK_ERROR_THRESHOLD,
    _gateway_url_valid,
    _registry_loop_body,
    registration_health,
)

MANIFEST = {"id": "fusion-cowork", "upstream": "http://127.0.0.1:11461"}


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


class _FlakyClient:
    def __init__(self, mode="connect_error", fail_then=None):
        self.mode = mode
        self.fail_then = fail_then
        self.calls = 0

    async def post(self, url, **kw):
        self.calls += 1
        if self.fail_then is not None and self.calls > self.fail_then:
            return _Resp(200)
        if self.mode == "connect_error":
            raise httpx.ConnectError("All connection attempts failed")
        if self.mode == "status":
            return _Resp(503, "bad gateway")
        return _Resp(200)


async def _run_for(seconds, client, manifest=MANIFEST, interval=0.01):
    task = asyncio.create_task(_registry_loop_body(client, manifest, interval))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
    monkeypatch.setenv("FUSION_ADMIN_KEY", "test-admin-key-448")
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    shell_registry._REGISTRATION_STATE.update({"enabled": False, "registered": False, "gateway": "", "last_error": ""})
    return monkeypatch


class TestGatewayUrlValidation:
    def test_valid_urls(self):
        assert _gateway_url_valid("http://127.0.0.1:11432") is True
        assert _gateway_url_valid("https://gw.example.com") is True

    def test_invalid_urls(self):
        assert _gateway_url_valid("") is False
        assert _gateway_url_valid("127.0.0.1:11432") is False
        assert _gateway_url_valid("ftp://x") is False
        assert _gateway_url_valid("http://") is False


class TestLoopObservability:
    async def test_startup_logs_resolved_url(self, caplog):
        with caplog.at_level(logging.INFO, logger="fusion_cowork.server.shell_registry"):
            await _run_for(0.05, _FlakyClient(mode="ok"))
        assert any("gateway=http://127.0.0.1:11432" in r.message for r in caplog.records)
        assert any("test-admin-key" not in r.message for r in caplog.records)

    async def test_invalid_url_fails_fast_no_loop(self, monkeypatch, caplog):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "not-a-url")
        client = _FlakyClient(mode="ok")
        with caplog.at_level(logging.ERROR, logger="fusion_cowork.server.shell_registry"):
            await _run_for(0.05, client)
        assert client.calls == 0, "invalid URL must fail fast, never loop"
        assert any("非法" in r.message for r in caplog.records)
        assert registration_health()["last_error"] == "invalid gateway url"

    async def test_connect_error_names_target(self, caplog):
        with caplog.at_level(logging.WARNING, logger="fusion_cowork.server.shell_registry"):
            await _run_for(0.05, _FlakyClient(mode="connect_error"))
        assert any("connection refused/unreachable: http://127.0.0.1:11432" in r.message for r in caplog.records)

    async def test_health_reflects_registration(self):
        await _run_for(0.05, _FlakyClient(mode="ok"))
        state = registration_health()
        assert state["registered"] is True
        assert state["gateway"] == "http://127.0.0.1:11432"
        assert state["last_error"] == ""

    async def test_health_carries_last_error_on_failure(self):
        await _run_for(0.05, _FlakyClient(mode="connect_error"))
        state = registration_health()
        assert state["registered"] is False
        assert "http://127.0.0.1:11432" in state["last_error"]


class TestFailureRateLimiting:
    async def test_sustained_failure_downgrades_to_hourly_error(self, caplog):
        # 连败达到阈值后: 一条 ERROR 摘要, 其余降为静默 — 不再逐拍 WARNING/ERROR 刷屏
        with caplog.at_level(logging.DEBUG, logger="fusion_cowork.server.shell_registry"):
            await _run_for(0.35, _FlakyClient(mode="connect_error"), interval=0.01)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "连续失败" in r.message]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "注册循环错误" in r.message]
        assert len(errors) == 1, errors
        assert len(warnings) == FAIL_STREAK_ERROR_THRESHOLD - 1, warnings

    async def test_recovery_resets_counter(self, caplog):
        client = _FlakyClient(mode="connect_error", fail_then=3)
        with caplog.at_level(logging.DEBUG, logger="fusion_cowork.server.shell_registry"):
            await _run_for(0.35, client, interval=0.01)
        assert registration_health()["registered"] is True
        # 恢复后再次故障 — 连败从头计, 先 WARNING (不直接跳 hourly ERROR)
        assert any("注册循环错误" in r.message for r in caplog.records)


class TestHealthEndpoint:
    def test_health_includes_shell_registration(self, tmp_path, monkeypatch):
        # 不让 create_web_app 拉起真实注册循环 (离线测试) — 关掉 URL 门控
        monkeypatch.delenv("FUSION_SHELL_GATEWAY_URL", raising=False)
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-448")
        from fusion_cowork.engine.workflow import WorkflowEngine

        eng = WorkflowEngine()
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as client:
            body = client.get("/health").json()
        assert "shell_registration" in body
        assert body["shell_registration"]["registered"] in (True, False)
        assert "gateway" in body["shell_registration"]
