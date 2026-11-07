"""issue #308: claude_dispatch node — sandboxed auto-dispatch of coder sessions.

Hard guardrails under test:
- HIGH_RISK_NODES registration (guard evaluates per execution)
- token + wall-clock budget caps; exceeded -> task issue back to state:ready + notification, never silent
- session crash -> back-to-ready path; timeout -> detach (no kill) + background watcher reap (#358)
- output must land as PR (no PR URL in result -> FAILED + back-to-ready)
"""

import asyncio
import json
import stat

import httpx
import pytest

from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.ops_nodes import _DETACHED_WATCHERS, ClaudeDispatchNode

import_all_nodes()


def _fake_claude(tmp_path, lines, exit_code=0, sleep=0):
    script = tmp_path / "fake-claude.sh"
    body = "\n".join(lines)
    script.write_text(f"#!/bin/bash\n{body}\nsleep {sleep}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _node(tmp_path, claude_bin, timeout=30, max_tokens=200000):
    node = ClaudeDispatchNode()
    node.config.params = {
        "repo": "org/repo",
        "allowed_repos": ["org/repo"],
        "issue_number": 9,
        "prompt": "fix the bug",
        "cwd": str(tmp_path),
        "claude_bin": claude_bin,
        "timeout": timeout,
        "max_tokens": max_tokens,
    }
    return node


def _capture_client(monkeypatch, requests):
    monkeypatch.setenv("GITHUB_TOKEN", "t308")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    def _fake_api_client(self, base_url, token, timeout):
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(ClaudeDispatchNode, "_api_client", _fake_api_client)


_OK_RESULT = json.dumps(
    {
        "result": "done, opened PR https://github.com/org/repo/pull/42 closes #9",
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }
)


class TestClaudeDispatch:
    @pytest.mark.asyncio
    async def test_success_pr_extracted(self, tmp_path, monkeypatch):
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, [f"echo '{_OK_RESULT}'"])
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "success"
        assert res.data["pr_url"] == "https://github.com/org/repo/pull/42"
        assert res.data["pr_number"] == 42
        assert res.data["total_tokens"] == 300
        assert requests == []  # no back-to-ready on success

    @pytest.mark.asyncio
    async def test_no_pr_back_to_ready(self, tmp_path, monkeypatch):
        requests = []
        _capture_client(monkeypatch, requests)
        no_pr = json.dumps({"result": "change done but no PR", "usage": {"input_tokens": 10, "output_tokens": 10}})
        binp = _fake_claude(tmp_path, [f"echo '{no_pr}'"])
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert "PR" in res.error
        assert ("POST", "/repos/org/repo/issues/9/labels") in requests
        assert ("DELETE", "/repos/org/repo/issues/9/labels/state:dispatched") in requests
        assert res.data["notification"]

    @pytest.mark.asyncio
    async def test_token_budget_exceeded_back_to_ready(self, tmp_path, monkeypatch):
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, [f"echo '{_OK_RESULT}'"])
        res = await _node(tmp_path, binp, max_tokens=100).execute({})
        assert res.status.value == "failed"
        assert "token" in res.error.lower()
        assert ("POST", "/repos/org/repo/issues/9/labels") in requests

    @pytest.mark.asyncio
    async def test_crash_back_to_ready(self, tmp_path, monkeypatch):
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, ["echo boom >&2"], exit_code=1)
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert ("POST", "/repos/org/repo/issues/9/labels") in requests
        assert res.data["notification"]

    @pytest.mark.asyncio
    async def test_params_schema_timeout_default_3600(self):
        # issue #358: 1800s 会硬杀真实交付会话 — 缺省提到 3600
        schema = ClaudeDispatchNode().get_params_schema()
        assert schema["properties"]["timeout"]["default"] == 3600

    @pytest.mark.asyncio
    async def test_timeout_detaches_without_kill(self, tmp_path, monkeypatch):
        # issue #358: 超时不 kill — 会话 detach 继续执行, 标签保持 dispatched,
        # watcher 收割终局 (无 PR -> 回退 state:ready)
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, ["echo 'no pr here'"], sleep=0.6)
        res = await _node(tmp_path, binp, timeout=0.3).execute({})
        assert res.status.value == "failed"
        assert "超时" in res.error
        assert res.data["detached"] is True
        assert res.data["pid"] > 0
        assert requests == []  # 标签不立即回退 — 会话仍在跑, 防重复派发
        await asyncio.gather(*list(_DETACHED_WATCHERS))
        assert ("POST", "/repos/org/repo/issues/9/labels") in requests  # watcher 收割后回退

    @pytest.mark.asyncio
    async def test_detached_session_delivers_pr(self, tmp_path, monkeypatch):
        # issue #358: detach 会话最终交付 PR — watcher 留痕, 不回退标签 (PR 已存在可回收)
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, [f"echo '{_OK_RESULT}'"], sleep=0.6)
        res = await _node(tmp_path, binp, timeout=0.3).execute({})
        assert res.status.value == "failed"
        assert res.data["detached"] is True
        await asyncio.gather(*list(_DETACHED_WATCHERS))
        assert requests == []  # PR 已交付 — 不回退 state:ready

    @pytest.mark.asyncio
    async def test_default_timeout_env_override(self, tmp_path, monkeypatch):
        # issue #358: 参数缺省时 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 生效 (小值触发超时路径验证)
        monkeypatch.setenv("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT", "0.05")
        requests = []
        _capture_client(monkeypatch, requests)
        binp = _fake_claude(tmp_path, ["echo hi"], sleep=0.5)
        node = _node(tmp_path, binp)
        node.config.params.pop("timeout")
        res = await node.execute({})
        assert res.status.value == "failed"
        assert res.data["detached"] is True
        await asyncio.gather(*list(_DETACHED_WATCHERS))

    @pytest.mark.asyncio
    async def test_repo_not_in_allowlist_rejected(self, tmp_path, monkeypatch):
        node = _node(tmp_path, "/bin/true")
        node.config.params["repo"] = "org/other"
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "白名单" in res.error

    def test_high_risk_registration(self):
        assert "claude_dispatch" in HIGH_RISK_NODES

    @pytest.mark.asyncio
    async def test_back_to_ready_without_token_is_loud(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        node = _node(tmp_path, "/bin/true")
        ok = await node._back_to_ready("org/repo", 9, "state:ready", "state:dispatched")
        assert ok is False
