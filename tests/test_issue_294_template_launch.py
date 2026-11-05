"""issue #294: governance pipeline full automation.

1. G1/G5 decisions auto-sync to agent-studio (n1b/n6b http_request nodes, nested payload)
2. GUI one-click launch: POST /api/templates/{id}/launch — params form -> run
3. batch_audit labels parametrized (array stays list through ${param.labels})
"""

import time

import pytest
from fastapi.testclient import TestClient

from fusion_cowork.engine.node import (
    BaseNode,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager

import_all_nodes()


@register_node
class _LaunchProbe294(BaseNode):
    name = "launch_probe_294"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _lifecycle_tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "governance_issue_lifecycle")


def _sync_node(tpl, node_id):
    return next(n for n in tpl["workflow"]["nodes"] if n["id"] == node_id)


class TestG1G5SyncNodes:
    def test_lifecycle_has_10_nodes_linear(self):
        tpl = _lifecycle_tpl()
        nodes = tpl["workflow"]["nodes"]
        assert len(nodes) == 10
        edges = {e["source_id"]: e["target_id"] for e in tpl["workflow"]["edges"]}
        assert edges["n1"] == "n1b"
        assert edges["n1b"] == "n2"
        assert edges["n6"] == "n6b"
        assert edges["n6b"] == "n7"

    def test_g1_sync_body(self):
        body = _sync_node(_lifecycle_tpl(), "n1b")["config"]["params"]["body"]
        assert body["title"].startswith("G1 proposal ")
        assert body["requester"] == "cowork-agent"
        assert body["assigned_to"] == "owner"
        assert body["payload"] == {"run_id": "${run_id}", "gate": "G1", "kind": "proposal"}

    def test_g5_sync_body(self):
        body = _sync_node(_lifecycle_tpl(), "n6b")["config"]["params"]["body"]
        assert body["title"].startswith("G5 rollout ")
        assert body["payload"] == {"run_id": "${run_id}", "gate": "G5", "kind": "rollout"}

    def test_sync_nodes_substituted_like_g4(self):
        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle", param_values={"repo": "org/x", "issue_number": 9}
        )
        assert wf is not None
        assert wf.nodes["n1b"].config.params["body"]["title"] == "G1 proposal org/x#9"
        assert wf.nodes["n6b"].config.params["body"]["title"] == "G5 rollout org/x#9"
        for nid in ("n1b", "n4", "n6b"):
            assert wf.nodes[nid].config.params["url"] == "http://127.0.0.1:11442/v1/agent-platform/approvals"


class TestBatchAuditLabelsParam:
    def test_labels_schema_declared(self):
        tpl = next(t for t in BUILTIN_TEMPLATES if t["id"] == "governance_batch_audit")
        spec = tpl["params"]["properties"]["labels"]
        assert spec["type"] == "array"
        assert spec["default"] == ["state:approved"]

    def test_labels_default_stays_list(self):
        wf = TemplateManager().template_to_workflow("governance_batch_audit")
        assert wf.nodes["n1"].config.params["labels"] == ["state:approved"]

    def test_labels_custom_values(self):
        wf = TemplateManager().template_to_workflow(
            "governance_batch_audit", param_values={"labels": ["state:approved", "priority:high"]}
        )
        assert wf.nodes["n1"].config.params["labels"] == ["state:approved", "priority:high"]


class TestLaunchRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-294")
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as c:
            yield c

    AUTH = {"Authorization": "Bearer test-key-294"}

    def _publish_probe_template(self, client, param_props):
        wf = {"name": "probe-294", "nodes": [{"id": "n1", "name": "launch_probe_294", "config": {}}], "edges": []}
        client.post(
            "/api/templates",
            json={"name": "launch probe 294", "workflow": wf, "params": param_props},
            headers=self.AUTH,
        )
        return "launch-probe-294"

    def test_launch_starts_run(self, client):
        _LaunchProbe294.calls = 0
        tpl_id = self._publish_probe_template(client, {})
        res = client.post(f"/api/templates/{tpl_id}/launch", json={"params": {}}, headers=self.AUTH)
        assert res.status_code == 202, res.text
        run_id = res.json()["run_id"]
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=self.AUTH).json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        assert _LaunchProbe294.calls == 1

    def test_launch_params_substituted(self, client, tmp_path):
        # param value lands in node config — verify via stored workflow_def
        wf = {
            "name": "probe-params-294",
            "nodes": [{"id": "n1", "name": "launch_probe_294", "config": {"params": {"repo": "${param.repo}"}}}],
            "edges": [],
        }
        client.post(
            "/api/templates",
            json={
                "name": "probe params 294",
                "workflow": wf,
                "params": {"type": "object", "properties": {"repo": {"type": "string", "default": "org/repo"}}},
            },
            headers=self.AUTH,
        )
        res = client.post(
            "/api/templates/probe-params-294/launch", json={"params": {"repo": "org/launched"}}, headers=self.AUTH
        )
        assert res.status_code == 202
        run_id = res.json()["run_id"]

        rec = client.get(f"/api/runs/{run_id}", headers=self.AUTH).json()
        node_params = rec["workflow_def"]["nodes"]["n1"]["config"]["params"]
        assert node_params["repo"] == "org/launched"

    def test_launch_missing_template_404(self, client):
        res = client.post("/api/templates/no-such-tpl-294/launch", json={"params": {}}, headers=self.AUTH)
        assert res.status_code == 404

    def test_launch_missing_required_param_400(self, client):
        wf = {
            "name": "probe-req-294",
            "nodes": [
                {
                    "id": "n1",
                    "name": "launch_probe_294",
                    "config": {"params": {"repo": "${param.repo}"}},
                }
            ],
            "edges": [],
        }
        client.post(
            "/api/templates",
            json={
                "name": "probe req 294",
                "workflow": wf,
                "params": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]},
            },
            headers=self.AUTH,
        )
        res = client.post("/api/templates/probe-req-294/launch", json={"params": {}}, headers=self.AUTH)
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "params_missing"

    def test_launch_governance_batch_audit_defaults(self, client):
        # batch audit with schema defaults — labels list survives substitution, launch accepted
        res = client.post(
            "/api/templates/governance_batch_audit/launch",
            json={"params": {"repo": "dahai80/fusion-cowork", "issue_number": 1}},
            headers=self.AUTH,
        )
        assert res.status_code == 202, res.text
        run_id = res.json()["run_id"]
        rec = client.get(f"/api/runs/{run_id}", headers=self.AUTH).json()
        n1 = next(n for n in rec["workflow_def"]["nodes"].values() if n["id"] == "n1")
        assert n1["config"]["params"]["labels"] == ["state:approved"]
        # background run will hit github/gates — cancel immediately, wire shape verified
        client.post(f"/api/runs/{run_id}/cancel", headers=self.AUTH)
