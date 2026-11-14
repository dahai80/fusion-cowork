"""issue #485: templates loadable/registerable through the web API.

- _seed_template_workflows: builtin (+ custom-dir) templates registered as
  workflow definitions at serve startup — product_lifecycle + its
  product_lifecycle_agents alias present on a fresh store, idempotent,
  refresh-on-template-change, never clobbers operator-created records
- POST /api/templates/{id}/install: on-demand registration with param assembly
- POST /api/runs workflow_id=<seeded alias>: kickoff works on a fresh deploy;
  per-run params re-assemble the source template before execution
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from fusion_cowork.engine.node import BaseNode, NodeResult, NodeStatus, register_node
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.server.web_api import WorkflowDefStore, _seed_template_workflows
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES

import_all_nodes()


@register_node
class _SeedProbe485(BaseNode):
    name = "seed_probe_485"

    async def execute(self, inputs: dict) -> NodeResult:
        repo = (getattr(self.config, "params", {}) or {}).get("repo")
        return NodeResult(status=NodeStatus.SUCCESS, data={"repo": repo}, summary="ok")


def _lifecycle_tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


class TestSeedHelper:
    def test_product_lifecycle_alias_declared(self):
        assert _lifecycle_tpl().get("web_aliases") == ["product_lifecycle_agents"]

    def test_seed_registers_lifecycle_and_alias(self, tmp_path):
        store = WorkflowDefStore(root=tmp_path / "store")
        seeded = _seed_template_workflows(store)
        assert "product_lifecycle" in seeded
        assert "product_lifecycle_agents" in seeded
        rec = store.get("product_lifecycle_agents")
        assert rec["template_id"] == "product_lifecycle"
        assert rec["alias_of"] == "product_lifecycle"
        assert rec["source"] == "template_seed"
        assert len(rec["workflow"]["nodes"]) == len(_lifecycle_tpl()["workflow"]["nodes"])
        assert "${param." not in json.dumps(rec["workflow"], ensure_ascii=False)

    def test_seed_idempotent(self, tmp_path):
        store = WorkflowDefStore(root=tmp_path / "store")
        assert _seed_template_workflows(store)
        assert _seed_template_workflows(store) == []

    def test_seed_refresh_on_template_change(self, tmp_path):
        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir()
        tpl = {
            "id": "custom_485",
            "name": "custom 485",
            "workflow": {
                "name": "custom-485",
                "nodes": [{"id": "n1", "name": "seed_probe_485", "config": {"params": {"repo": "org/v1"}}}],
                "edges": [],
            },
        }
        (tpl_dir / "custom_485.json").write_text(json.dumps(tpl, ensure_ascii=False), encoding="utf-8")
        store = WorkflowDefStore(root=tmp_path / "store")
        assert "custom_485" in _seed_template_workflows(store, tpl_dir)
        assert store.get("custom_485")["workflow"]["nodes"]["n1"]["config"]["params"]["repo"] == "org/v1"
        tpl["workflow"]["nodes"][0]["config"]["params"]["repo"] = "org/v2"
        (tpl_dir / "custom_485.json").write_text(json.dumps(tpl, ensure_ascii=False), encoding="utf-8")
        assert "custom_485" in _seed_template_workflows(store, tpl_dir)
        assert store.get("custom_485")["workflow"]["nodes"]["n1"]["config"]["params"]["repo"] == "org/v2"

    def test_seed_no_clobber_operator_record(self, tmp_path):
        store = WorkflowDefStore(root=tmp_path / "store")
        operator_wf = {"name": "op-485", "nodes": [{"id": "a", "name": "merge", "config": {}}], "edges": []}
        saved = store.save("product_lifecycle", {"name": "operator flow", "workflow": operator_wf})
        _seed_template_workflows(store)
        assert store.get("product_lifecycle") == saved

    def test_seed_raw_when_required_param_missing(self, tmp_path):
        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir()
        tpl = {
            "id": "req_485",
            "name": "req 485",
            "workflow": {
                "name": "req-485",
                "nodes": [{"id": "n1", "name": "seed_probe_485", "config": {"params": {"repo": "${param.repo}"}}}],
                "edges": [],
            },
            "params": {"type": "object", "properties": {"repo": {"type": "string"}}},
        }
        (tpl_dir / "req_485.json").write_text(json.dumps(tpl, ensure_ascii=False), encoding="utf-8")
        store = WorkflowDefStore(root=tmp_path / "store")
        assert "req_485" in _seed_template_workflows(store, tpl_dir)
        # raw workflow kept (no defaults to bake) — /api/runs params path assembles it
        assert "${param.repo}" in json.dumps(store.get("req_485")["workflow"], ensure_ascii=False)


class TestSeedIntegration:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-485")
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "store",
            templates_dir=tmp_path / "tpl",
        )
        with TestClient(app) as c:
            yield c

    AUTH = {"Authorization": "Bearer test-key-485"}

    def _publish_probe_template(self, client):
        wf = {
            "name": "probe-485",
            "nodes": [{"id": "n1", "name": "seed_probe_485", "config": {"params": {"repo": "${param.repo}"}}}],
            "edges": [],
        }
        res = client.post(
            "/api/templates",
            json={
                "name": "probe 485",
                "workflow": wf,
                "params": {"type": "object", "properties": {"repo": {"type": "string", "default": "org/seed"}}},
            },
            headers=self.AUTH,
        )
        assert res.status_code in (200, 201), res.text
        return res.json()["id"]

    def test_workflows_list_includes_seeded(self, client):
        ids = {w["id"] for w in client.get("/api/workflows", headers=self.AUTH).json()["workflows"]}
        assert "product_lifecycle" in ids
        assert "product_lifecycle_agents" in ids

    def test_run_by_seeded_alias_starts(self, client):
        res = client.post(
            "/api/runs",
            json={
                "workflow_id": "product_lifecycle_agents",
                "source": "fusion-site-factory",
                "labels": {"spec_id": "s-485", "order_id": "o-485"},
            },
            headers=self.AUTH,
        )
        assert res.status_code == 202, res.text
        assert res.json()["run_id"]
        client.post(f"/api/runs/{res.json()['run_id']}/cancel", headers=self.AUTH)

    def test_install_registers_definition(self, client):
        tpl_id = self._publish_probe_template(client)
        res = client.post(f"/api/templates/{tpl_id}/install", json={}, headers=self.AUTH)
        assert res.status_code == 201, res.text
        assert tpl_id in res.json()["installed"]
        rec = client.get(f"/api/workflows/{tpl_id}", headers=self.AUTH).json()
        assert rec["workflow"]["nodes"]["n1"]["config"]["params"]["repo"] == "org/seed"

    def test_install_unknown_template_404(self, client):
        res = client.post("/api/templates/no-such-485/install", json={}, headers=self.AUTH)
        assert res.status_code == 404

    def test_install_missing_required_param_400(self, client, tmp_path):
        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir(exist_ok=True)
        tpl = {
            "id": "req2_485",
            "name": "req2 485",
            "workflow": {
                "name": "req2-485",
                "nodes": [{"id": "n1", "name": "seed_probe_485", "config": {"params": {"repo": "${param.repo}"}}}],
                "edges": [],
            },
            "params": {"type": "object", "properties": {"repo": {"type": "string"}}},
        }
        (tpl_dir / "req2_485.json").write_text(json.dumps(tpl, ensure_ascii=False), encoding="utf-8")
        res = client.post("/api/templates/req2_485/install", json={}, headers=self.AUTH)
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "params_missing"

    def test_run_params_reassemble_template(self, client):
        tpl_id = self._publish_probe_template(client)
        assert client.post(f"/api/templates/{tpl_id}/install", json={}, headers=self.AUTH).status_code == 201
        res = client.post(
            "/api/runs",
            json={"workflow_id": tpl_id, "params": {"repo": "org/per-run"}},
            headers=self.AUTH,
        )
        assert res.status_code == 202, res.text
        run_id = res.json()["run_id"]
        detail = {"status": "running"}
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=self.AUTH).json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        node_params = detail["workflow_def"]["nodes"]["n1"]["config"]["params"]
        assert node_params["repo"] == "org/per-run"

    def test_run_params_on_inline_workflow_400(self, client):
        wf = {"name": "inline-485", "nodes": [{"id": "n1", "name": "seed_probe_485", "config": {}}], "edges": []}
        res = client.post("/api/runs", json={"workflow": wf, "params": {"repo": "x"}}, headers=self.AUTH)
        assert res.status_code == 400
        assert "template_id" in res.json()["detail"]["message"]
