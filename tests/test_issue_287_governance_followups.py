"""issue #287: governance template pack follow-ups.

1. ${env.VAR} template prefix on http_request (auth header from env, never hardcoded)
2. unresolved credential headers dropped (template literal never sent as secret)
3. template-level params (${param.X}) substituted at template-assembly time
4. CLI template run path registers all node modules (import_all_nodes)
5. confirm {"once": true} without explicit approved defaults to approve-once
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.tools.http_request_node import HttpRequestNode, resolve_template

import_all_nodes()

ENV_KEY = "FUSION_TEST_287_API_KEY"


# ── 1+2: http_request env: prefix + credential header safety ──


class _CaptureClient:
    captured = {}

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        type(self).captured = {"method": method, "url": url, **kw}

        class _Resp:
            status_code = 200
            text = "{}"
            headers = {}

            def json(self):
                return {}

        return _Resp()


def _make_node(params: dict) -> HttpRequestNode:
    node = NodeRegistry.create("http_request", node_id="hr", config=NodeConfig(params=params))
    node._client = lambda timeout: _CaptureClient()
    return node


def test_env_prefix_resolves_from_environment(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "secret-287")
    ctx = {}
    assert resolve_template(f"${{env.{ENV_KEY}}}", ctx) == "secret-287"


def test_env_prefix_missing_keeps_literal(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    ctx = {}
    assert resolve_template(f"${{env.{ENV_KEY}}}", ctx) == f"${{env.{ENV_KEY}}}"


def test_env_auth_header_sent(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "secret-287")
    _CaptureClient.captured = {}
    node = _make_node(
        {
            "url": "http://127.0.0.1:1/x",
            "method": "POST",
            "headers": {"x-api-key": f"${{env.{ENV_KEY}}}"},
        }
    )
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS
    assert _CaptureClient.captured["headers"]["x-api-key"] == "secret-287"


def test_unresolved_credential_header_dropped(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    _CaptureClient.captured = {}
    node = _make_node(
        {
            "url": "http://127.0.0.1:1/x",
            "method": "POST",
            "headers": {"x-api-key": f"${{env.{ENV_KEY}}}"},
            "body": {"note": f"${{env.{ENV_KEY}}}"},
        }
    )
    result = asyncio.run(node.execute({}))
    assert result.status == NodeStatus.SUCCESS
    assert "x-api-key" not in _CaptureClient.captured["headers"]
    # body keeps the literal (back-compat, observable) — only credential headers drop
    assert _CaptureClient.captured["json"]["note"] == f"${{env.{ENV_KEY}}}"


# ── 3: template params ──


class TestTemplateParams:
    def test_governance_template_declares_params(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        tpl = TemplateManager().get_template("governance_issue_lifecycle")
        assert tpl is not None
        props = tpl["params"]["properties"]
        assert {"repo", "issue_number", "approvals_url"} <= set(props)

    def test_param_values_substituted(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle",
            param_values={"repo": "dahai80/fusion-cowork", "issue_number": 42},
        )
        assert wf is not None
        n1 = wf.nodes["n1"].config.params
        assert n1["repo"] == "dahai80/fusion-cowork"
        assert n1["allowed_repos"] == ["dahai80/fusion-cowork"]
        assert n1["issue_number"] == 42
        n4 = wf.nodes["n4"].config.params
        assert n4["url"] == "http://127.0.0.1:11442/v1/agent-platform/approvals"
        assert n4["headers"] == {"x-api-key": "${env.FUSION_AGENT_STUDIO_API_KEY}"}
        # issue #291: approvals API required fields — requester/assigned_to present
        # and distinct (no self-approval), title carries the parametrized target
        body = n4["body"]
        assert body["requester"] == "cowork-agent"
        assert body["assigned_to"] == "owner"
        assert body["requester"] != body["assigned_to"]
        assert body["title"] == "G4 acceptance dahai80/fusion-cowork#42"
        # issue #293: run context nested under payload — agent-studio persists only
        # body["payload"] as task params (top-level keys dropped, webhook can't route back)
        # issue #306: payload gains evidence {pr_url, ci_status} from the n4a linked_pr gate
        assert body["payload"] == {
            "run_id": "${run_id}",
            "gate": "G4",
            "kind": "acceptance",
            "evidence": {"pr_url": "${result.pr_url}", "ci_status": "${result.ci_status}"},
        }
        assert "run_id" not in body

    def test_schema_defaults_applied_without_values(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow("governance_issue_lifecycle")
        assert wf is not None
        n1 = wf.nodes["n1"].config.params
        assert n1["repo"] == "org/repo"
        assert n1["issue_number"] == 1
        # no literal ${param.*} residue anywhere
        for node in wf.nodes.values():
            assert "${param." not in json.dumps(node.config.params, ensure_ascii=False)

    def test_batch_audit_template_params(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow("governance_batch_audit", param_values={"repo": "org/other"})
        assert wf.nodes["n1"].config.params["repo"] == "org/other"

    def test_no_param_values_leaves_other_templates_untouched(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow("desktop_daily_cleanup")
        assert wf is not None


# ── 4: CLI template run registers nodes ──


def test_cli_template_run_imports_all_nodes(capsys):
    from fusion_cowork import cli
    from fusion_cowork.engine.node import NodeRegistry

    asyncio.run(cli._async_run_template("governance_issue_lifecycle", True, "", "", ""))
    for name in ("github_api", "approval_gate", "http_request"):
        assert NodeRegistry.get(name) is not None, name
    capsys.readouterr()


# ── 5: confirm once defaults to approve ──


@register_node
class _GateProbe287(BaseNode):
    name = "gate_probe_287"
    display_name = "Gate Probe 287"
    category = NodeCategory.TOOL
    description = "test high-risk probe"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture
def pm_287(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {"gate_probe_287"})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def test_confirm_once_without_approved_defaults_to_approve(pm_287, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-287")
    eng = WorkflowEngine(permission_manager=pm_287)
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    _GateProbe287.calls = 0
    auth = {"Authorization": "Bearer test-key-287"}
    wf_def = {
        "name": "once-default-287",
        "workflow": {"nodes": [{"id": "n1", "name": "gate_probe_287", "config": {}}], "edges": []},
    }
    with TestClient(app) as client:
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
        for _ in range(200):
            if eng.hitl_pending(run_id):
                break
            time.sleep(0.05)
        assert eng.hitl_pending(run_id)
        action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
        # the exact body from the #287 observation — no "approved" field
        confirm = client.post(f"/api/permissions/pending/{action_id}/confirm", json={"once": True}, headers=auth)
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["approved"] is True
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        assert _GateProbe287.calls == 1


def test_explicit_deny_once_still_denies(pm_287, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-287")
    eng = WorkflowEngine(permission_manager=pm_287)
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    _GateProbe287.calls = 0
    auth = {"Authorization": "Bearer test-key-287"}
    wf_def = {
        "name": "once-deny-287",
        "workflow": {"nodes": [{"id": "n1", "name": "gate_probe_287", "config": {}}], "edges": []},
    }
    with TestClient(app) as client:
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
        for _ in range(200):
            if eng.hitl_pending(run_id):
                break
            time.sleep(0.05)
        action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
        confirm = client.post(
            f"/api/permissions/pending/{action_id}/confirm", json={"approved": False, "once": True}, headers=auth
        )
        assert confirm.status_code == 200
        assert confirm.json()["approved"] is False
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "failed", detail
        assert _GateProbe287.calls == 0
