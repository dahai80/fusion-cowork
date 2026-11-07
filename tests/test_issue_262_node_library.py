"""issue #262: 官方通用节点库 — agent_exec / github_api / command_evidence / http_webhook / approval_gate。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys

import pytest

from fusion_cowork.engine.node import NodeRegistry, NodeStatus
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


def _node(name: str, params: dict | None = None):
    n = NodeRegistry.create(name, node_id=f"n_{name}")
    if params:
        n.config.params = params
    return n


async def _run_node(name: str, params: dict | None = None, inputs: dict | None = None):
    n = _node(name, params)
    return await n.execute(inputs or {})


class TestAgentExec:
    @pytest.mark.asyncio
    async def test_success_captures_output(self):
        res = await _run_node("agent_exec", {"command": [sys.executable, "-c", "print('hello-262')"]})
        assert res.status == NodeStatus.SUCCESS
        assert res.data["exit_code"] == 0
        assert "hello-262" in res.data["output"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_with_code(self):
        res = await _run_node("agent_exec", {"command": [sys.executable, "-c", "import sys; sys.exit(3)"]})
        assert res.status == NodeStatus.FAILED
        assert res.data["exit_code"] == 3

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        res = await _run_node(
            "agent_exec", {"command": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout": 0.3}
        )
        assert res.status == NodeStatus.FAILED
        assert "超时" in res.error

    @pytest.mark.asyncio
    async def test_bad_command_shape(self):
        res = await _run_node("agent_exec", {"command": "rm -rf /"})
        assert res.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_missing_cwd(self):
        res = await _run_node("agent_exec", {"command": [sys.executable, "-c", "pass"], "cwd": "/no/such/dir-262"})
        assert res.status == NodeStatus.FAILED
        assert "不存在" in res.error


class FakeGHClient:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, path, json=None, params=None):
        status, payload = self.status, self.payload

        class R:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    import httpx

                    raise httpx.HTTPStatusError("err", request=None, response=self)

            def json(self):
                return payload

            text = ""

        return R()


class TestGitHubApi:
    @pytest.mark.asyncio
    async def test_repo_whitelist_refusal(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        res = await _run_node(
            "github_api", {"op": "issue_create", "repo": "evil/x", "allowed_repos": ["org/ok"], "title": "t"}
        )
        assert res.status == NodeStatus.FAILED
        assert "白名单" in res.error

    @pytest.mark.asyncio
    async def test_token_missing(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        res = await _run_node(
            "github_api", {"op": "issue_create", "repo": "org/ok", "allowed_repos": ["org/ok"], "title": "t"}
        )
        assert res.status == NodeStatus.FAILED
        assert "GITHUB_TOKEN" in res.error

    @pytest.mark.asyncio
    async def test_issue_create_ok(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        n = _node(
            "github_api",
            {
                "op": "issue_create",
                "repo": "org/ok",
                "allowed_repos": ["org/ok"],
                "title": "bug",
                "labels": ["phase:dev"],
            },
        )
        n._client = lambda *a, **k: FakeGHClient(payload={"number": 1})
        res = await n.execute({})
        assert res.status == NodeStatus.SUCCESS
        assert res.data["result"]["number"] == 1

    @pytest.mark.asyncio
    async def test_http_error_surfaced(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        n = _node("github_api", {"op": "pr_get", "repo": "org/ok", "allowed_repos": ["org/ok"], "issue_number": 9})
        n._client = lambda *a, **k: FakeGHClient(status=404)
        res = await n.execute({})
        assert res.status == NodeStatus.FAILED
        assert "404" in res.error

    @pytest.mark.asyncio
    async def test_unknown_op(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        res = await _run_node("github_api", {"op": "force_push", "repo": "org/ok", "allowed_repos": ["org/ok"]})
        assert res.status == NodeStatus.FAILED


class TestCommandEvidence:
    @pytest.mark.asyncio
    async def test_allowlist_matches_versioned_interpreter(self, tmp_path):
        # issue #334: venv/pyenv 解释器常为绝对路径 python3.12 — basename 去版本后缀后
        # 与 `python *` 同族匹配, 不再依赖 PATH 里的解释器拼法
        fake_py = tmp_path / ".venv" / "bin" / "python3.12"
        fake_py.parent.mkdir(parents=True)
        fake_py.write_text("#!/bin/sh\n")
        fake_py.chmod(0o755)
        res = await _run_node(
            "command_evidence",
            {"command": [str(fake_py), "-c", "pass"], "allowlist": ["python *"], "evidence_dir": str(tmp_path / "ev")},
        )
        assert res.status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_allowlist_still_rejects_unrelated_binary(self, tmp_path):
        fake = tmp_path / "bin" / "curl"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\n")
        res = await _run_node(
            "command_evidence",
            {"command": [str(fake), "-s", "http://x"], "allowlist": ["python *"], "evidence_dir": str(tmp_path / "ev")},
        )
        assert res.status == NodeStatus.FAILED
        assert "白名单" in res.error

    @pytest.mark.asyncio
    async def test_allowlist_match_archives_scrubbed(self, tmp_path):
        ev = tmp_path / "evidence"
        cmd = [sys.executable, "-c", "print('token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 done')"]
        res = await _run_node("command_evidence", {"command": cmd, "allowlist": ["python *"], "evidence_dir": str(ev)})
        assert res.status == NodeStatus.SUCCESS
        text = (ev / res.data["evidence_path"].rsplit("/", 1)[-1]).read_text()
        assert "ghp_" not in text
        assert "[REDACTED]" in text

    @pytest.mark.asyncio
    async def test_allowlist_refusal_loud(self, tmp_path):
        res = await _run_node(
            "command_evidence",
            {"command": [sys.executable, "-c", "pass"], "allowlist": ["git *"], "evidence_dir": str(tmp_path)},
        )
        assert res.status == NodeStatus.FAILED
        assert "白名单" in res.error

    @pytest.mark.asyncio
    async def test_empty_allowlist_refused(self, tmp_path):
        res = await _run_node(
            "command_evidence",
            {"command": [sys.executable, "-c", "pass"], "allowlist": [], "evidence_dir": str(tmp_path)},
        )
        assert res.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_port_reconciliation(self, tmp_path):
        reg = tmp_path / "ports.json"
        reg.write_text(json.dumps({"1": False}), encoding="utf-8")
        res = await _run_node(
            "command_evidence",
            {
                "command": [sys.executable, "-c", "pass"],
                "allowlist": ["python *"],
                "evidence_dir": str(tmp_path),
                "ports": [1],
                "port_registry": str(reg),
            },
        )
        assert res.status == NodeStatus.SUCCESS
        assert res.data["ports"][0]["port"] == 1
        assert res.data["ports"][0]["expected"] is False


class TestHttpWebhook:
    def _patch_client(self, monkeypatch, handler):
        import httpx as _httpx

        import fusion_cowork.nodes.ops.ops_nodes as mod

        real_client = _httpx.AsyncClient

        def factory(**kwargs):
            return real_client(transport=_httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5))

        monkeypatch.setattr(mod.httpx, "AsyncClient", factory)

    @pytest.mark.asyncio
    async def test_post_ok_signed(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEBHOOK_SECRET", "s262")
        seen = {}

        def handler(request):
            seen["sig"] = request.headers.get("X-Fusion-Signature", "")
            seen["body"] = request.content
            import httpx as _httpx

            return _httpx.Response(200, json={"ok": True})

        self._patch_client(monkeypatch, handler)
        res = await _run_node("http_webhook", {"url": "https://x.example/hook", "body": {"a": 1}})
        assert res.status == NodeStatus.SUCCESS
        assert res.data["status_code"] == 200
        expect = "sha256=" + hmac.new(b"s262", seen["body"], hashlib.sha256).hexdigest()
        assert seen["sig"] == expect

    @pytest.mark.asyncio
    async def test_5xx_retries_then_success(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            import httpx as _httpx

            if calls["n"] < 2:
                return _httpx.Response(503)
            return _httpx.Response(200, json={"ok": 1})

        self._patch_client(monkeypatch, handler)
        res = await _run_node("http_webhook", {"url": "https://x.example/hook", "retry": 2, "retry_backoff": 0})
        assert res.status == NodeStatus.SUCCESS
        assert res.data["attempts"] == 2

    @pytest.mark.asyncio
    async def test_4xx_no_retry(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            import httpx as _httpx

            return _httpx.Response(400, json={"e": "bad"})

        self._patch_client(monkeypatch, handler)
        res = await _run_node("http_webhook", {"url": "https://x.example/hook", "retry": 3, "retry_backoff": 0})
        assert res.status == NodeStatus.FAILED
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_bad_scheme_refused(self):
        res = await _run_node("http_webhook", {"url": "ftp://x"})
        assert res.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_env_header_interpolation(self, monkeypatch):
        monkeypatch.setenv("T262", "tok-262")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization", "")
            import httpx as _httpx

            return _httpx.Response(200, json={})

        self._patch_client(monkeypatch, handler)
        res = await _run_node(
            "http_webhook", {"url": "https://x.example/hook", "headers": {"Authorization": "${T262}"}}
        )
        assert res.status == NodeStatus.SUCCESS
        assert seen["auth"] == "tok-262"

    @pytest.mark.asyncio
    async def test_env_header_missing_fails(self, monkeypatch):
        monkeypatch.delenv("T262_MISSING", raising=False)
        res = await _run_node(
            "http_webhook", {"url": "https://x.example/hook", "headers": {"Authorization": "${T262_MISSING}"}}
        )
        assert res.status == NodeStatus.FAILED
        assert "环境变量" in res.error


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_gate_in_high_risk_set(self):
        from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES

        assert "approval_gate" in HIGH_RISK_NODES
        assert "agent_exec" in HIGH_RISK_NODES
        assert "command_evidence" in HIGH_RISK_NODES

    @pytest.mark.asyncio
    async def test_gate_pauses_then_resumes(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = Workflow(name="wf-262")
        wf.add_node(_node("approval_gate", {"prompt": "发布审批"}))
        task = asyncio.create_task(eng.execute(wf, execution_id="run_gate262"))
        for _ in range(200):
            if eng.hitl_pending("run_gate262"):
                break
            await asyncio.sleep(0.01)
        assert eng.hitl_pending("run_gate262"), "approval_gate 应天生进 HITL 等待"
        eng.resolve_hitl("run_gate262", True, once=True)
        execution = await task
        assert execution.status.value == "success"
        assert execution.steps[-1].output_data.get("approved") is True


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k262")
    app = web_api.create_web_app(dist_dir=tmp_path / "nd", store_root=tmp_path / "wf", space_data_dir=tmp_path / "sp")
    return TestClient(app)


class TestWebhookRoute:
    def test_no_secret_503(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_COWORK_WEBHOOK_SECRET", raising=False)
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post("/api/hooks/webhook/run_x", json={"approved": True})
            assert r.status_code == 503

    def test_bad_signature_401(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_WEBHOOK_SECRET", "s262")
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post(
                "/api/hooks/webhook/run_x",
                content=json.dumps({"approved": True}).encode(),
                headers={"X-Fusion-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
            )
            assert r.status_code == 401

    def test_valid_signature_no_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_WEBHOOK_SECRET", "s262")
        tc = _client(monkeypatch, tmp_path)
        with tc:
            body = json.dumps({"approved": True}).encode()
            sig = "sha256=" + hmac.new(b"s262", body, hashlib.sha256).hexdigest()
            r = tc.post(
                "/api/hooks/webhook/run_nopending",
                content=body,
                headers={"X-Fusion-Signature": sig, "Content-Type": "application/json"},
            )
            assert r.status_code == 200
            assert r.json()["resumed"] is False
