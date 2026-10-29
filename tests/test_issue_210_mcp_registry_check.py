"""issue #210: mcp_client 注册表审计 — 握手前对照 mcp-server-registry.json, warn 级不阻断。

两组: sys.modules 注入假 fusion_core (CI 裸装也跑) + 真实 fusion-core 走临时注册表
(pytest.importorskip 守卫), 校验 verdict 分类、correlation_id、审计日志与放行语义。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import types

import httpx
import pytest

from fusion_cowork.engine.node import NodeConfig
from fusion_cowork.nodes.ecosystem.mcp_client_node import MCPClientNode

LOGGER_NAME = "fusion_cowork.nodes.ecosystem.mcp_client_node"
_TRACE_RE = re.compile(r"correlation_id=fc_[0-9a-f]{16}")


def _make_node(params=None):
    return MCPClientNode(config=NodeConfig(params=params or {}))


def _ok_transport(order=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if order is not None:
            order.append(body.get("method"))
        if body.get("method") == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s1"}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "done"}], "isError": False}},
        )

    return handler


def _bind(node, handler):
    node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)


def _verdict(action, server="sheets", reason="stub reason"):
    return types.SimpleNamespace(action=action, server=server, reason=reason)


def _fake_fusion_core(monkeypatch, check_approval):
    mod = types.ModuleType("fusion_core")
    mod.check_approval = check_approval
    monkeypatch.setitem(sys.modules, "fusion_core", mod)


def _logs(caplog, level):
    return [r.message for r in caplog.records if r.name == LOGGER_NAME and r.levelno == level]


class TestWarnLevelVerdicts:
    async def test_approved_logs_info_and_proceeds(self, monkeypatch, caplog):
        calls = []

        def check(url, tool_scope):
            calls.append((url, tool_scope))
            return _verdict("approved")

        _fake_fusion_core(monkeypatch, check)
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "sheets.read"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        assert calls == [("http://127.0.0.1:11500/mcp", "sheets.read")]
        infos = _logs(caplog, logging.INFO)
        assert any("registry approved" in m and "server=sheets" in m for m in infos)
        assert any(_TRACE_RE.search(m) for m in infos)
        assert not [m for m in _logs(caplog, logging.WARNING) if "registry" in m]

    async def test_denied_warns_with_reason_but_still_calls(self, monkeypatch, caplog):
        _fake_fusion_core(monkeypatch, lambda url, tool_scope: _verdict("denied", reason="status is 'reviewed'"))
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "sheets.read"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        warns = [m for m in _logs(caplog, logging.WARNING) if "registry denied" in m]
        assert warns
        assert "status is 'reviewed'" in warns[0]
        assert "tool=sheets.read" in warns[0]
        assert _TRACE_RE.search(warns[0])

    async def test_unknown_warns(self, monkeypatch, caplog):
        _fake_fusion_core(monkeypatch, lambda url, tool_scope: _verdict("unknown", server=None, reason="no match"))
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "x"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        warns = [m for m in _logs(caplog, logging.WARNING) if "registry unknown" in m]
        assert warns
        assert "reason=no match" in warns[0]

    async def test_check_runs_before_handshake(self, monkeypatch):
        order = []

        def check(url, tool_scope):
            order.append("check")
            return _verdict("approved")

        _fake_fusion_core(monkeypatch, check)
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "t"})
        _bind(node, _ok_transport(order=order))
        result = await node.execute({})
        assert result.status.value == "success"
        assert order[0] == "check"
        assert order[1] == "initialize"

    async def test_check_exception_warns_and_proceeds(self, monkeypatch, caplog):
        def boom(url, tool_scope):
            raise RuntimeError("registry file corrupt")

        _fake_fusion_core(monkeypatch, boom)
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "t"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        warns = [m for m in _logs(caplog, logging.WARNING) if "注册表检查失败" in m]
        assert warns
        assert "registry file corrupt" in warns[0]
        assert _TRACE_RE.search(warns[0])

    async def test_fusion_core_absent_skips_silently(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "fusion_core", None)
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "t"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        assert not [m for m in _logs(caplog, logging.WARNING) if "注册表" in m or "registry" in m]
        assert any("跳过注册表审计" in m for m in _logs(caplog, logging.DEBUG))


class TestRealFusionCore:
    @pytest.fixture
    def registry(self, monkeypatch, tmp_path):
        fusion_core = pytest.importorskip("fusion_core")
        from fusion_core import mcp_registry

        path = tmp_path / "mcp-server-registry.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [
                        {
                            "name": "sheets-prod",
                            "status": "approved",
                            "credential_mode": "env_ref",
                            "url_prefixes": ["http://127.0.0.1:11500/mcp"],
                            "tool_scopes": ["sheets.*"],
                        },
                        {
                            "name": "sheets-dev",
                            "status": "reviewed",
                            "credential_mode": "env_ref",
                            "url_prefixes": ["http://127.0.0.1:11501/mcp"],
                            "tool_scopes": ["sheets.*"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("FUSION_MCP_REGISTRY", str(path))
        mcp_registry.clear_cache()
        yield fusion_core
        mcp_registry.clear_cache()

    async def test_approved_server_info(self, registry, caplog):
        node = _make_node({"url": "http://127.0.0.1:11500/mcp", "tool": "sheets.read"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        assert any("registry approved server=sheets-prod" in m for m in _logs(caplog, logging.INFO))

    async def test_reviewed_server_denied_warning(self, registry, caplog):
        node = _make_node({"url": "http://127.0.0.1:11501/mcp", "tool": "sheets.read"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        warns = [m for m in _logs(caplog, logging.WARNING) if "registry denied server=sheets-dev" in m]
        assert warns
        assert "only approved servers may be referenced" in warns[0]

    async def test_unlisted_url_unknown_warning(self, registry, caplog):
        node = _make_node({"url": "http://127.0.0.1:11999/mcp", "tool": "x"})
        _bind(node, _ok_transport())
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            result = await node.execute({})
        assert result.status.value == "success"
        asserts = [m for m in _logs(caplog, logging.WARNING) if "registry unknown" in m]
        assert asserts
        assert "no registry server matches" in asserts[0]
