"""issue #516: workflow asset discipline.

- Versioning: every workflow save is an immutable asset version with a
  canonical digest; runs record which version + digest executed.
- Machine-checkable acceptance gates: node_count / required_nodes /
  forbidden_nodes / required_params evaluated at activation; unknown rule
  types fail closed.
- Regression gate (opt-in): candidate definition replays against recorded
  golden executions (per-node output digests) before activation; a mismatch
  leaves the previous version active.
- Audit trail: who changed what, when, with which digest and gate results —
  queryable independently of execution logs.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.server import web_api
from fusion_cowork.server.workflow_assets import (
    WorkflowAssetStore,
    canonical_digest,
    diff_versions,
    evaluate_acceptance,
)


@register_node
class _Echo516(BaseNode):
    name = "echo_516"
    display_name = "Echo 516"
    category = NodeCategory.TOOL
    description = "deterministic echo node for asset tests"
    icon = "🔁"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        say = str((self.config.params or {}).get("say", ""))
        return NodeResult(status=NodeStatus.SUCCESS, data={"echo": say}, summary=f"echo {say}")


def _wf_body(say: str = "hello", node_id: str = "n1") -> dict:
    return {
        "name": "asset-516",
        "workflow": {
            "nodes": [{"id": node_id, "name": "echo_516", "config": {"params": {"say": say}}}],
            "edges": [],
        },
    }


class TestCanonicalDigest:
    def test_stable_and_key_order_insensitive(self):
        a = canonical_digest({"x": 1, "y": {"b": 2, "a": 1}})
        b = canonical_digest({"y": {"a": 1, "b": 2}, "x": 1})
        assert a == b
        assert len(a) == 16

    def test_different_body_different_digest(self):
        assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})


class TestAcceptanceRules:
    def _wf(self, nodes: list) -> dict:
        return {"nodes": nodes, "edges": []}

    def test_node_count_pass_and_fail(self):
        wf = self._wf([{"id": "a", "name": "echo_516"}])
        ok, res = evaluate_acceptance([{"type": "node_count", "min": 1, "max": 3}], wf)
        assert ok and res[0]["ok"]
        ok, res = evaluate_acceptance([{"type": "node_count", "min": 2}], wf)
        assert not ok and "node count 1" in res[0]["detail"]

    def test_required_and_forbidden_nodes(self):
        wf = self._wf([{"id": "a", "name": "echo_516"}, {"id": "b", "name": "shell_exec"}])
        ok, _ = evaluate_acceptance([{"type": "required_nodes", "names": ["echo_516"]}], wf)
        assert ok
        ok, res = evaluate_acceptance([{"type": "required_nodes", "names": ["nope"]}], wf)
        assert not ok and "missing" in res[0]["detail"]
        ok, res = evaluate_acceptance([{"type": "forbidden_nodes", "names": ["shell_exec"]}], wf)
        assert not ok and "forbidden present" in res[0]["detail"]

    def test_required_params(self):
        wf = self._wf([{"id": "a", "name": "echo_516", "config": {"params": {"say": "x"}}}])
        ok, _ = evaluate_acceptance([{"type": "required_params", "node": "a", "params": ["say"]}], wf)
        assert ok
        ok, res = evaluate_acceptance([{"type": "required_params", "node": "a", "params": ["say", "gone"]}], wf)
        assert not ok and "gone" in res[0]["detail"]
        ok, res = evaluate_acceptance([{"type": "required_params", "node": "zz", "params": ["say"]}], wf)
        assert not ok and "not found" in res[0]["detail"]

    def test_unknown_rule_fails_closed(self):
        ok, res = evaluate_acceptance([{"type": "vibes_only"}], self._wf([]))
        assert not ok and res[0]["ok"] is False
        ok, res = evaluate_acceptance("not a list", self._wf([]))
        assert not ok


class TestDiffVersions:
    def test_node_and_edge_diff(self):
        old = {
            "nodes": [{"id": "a", "name": "x"}, {"id": "b", "name": "y"}],
            "edges": [{"source_id": "a", "target_id": "b"}],
        }
        new = {"nodes": [{"id": "a", "name": "x"}, {"id": "c", "name": "z"}], "edges": []}
        d = diff_versions(old, new)
        assert d["nodes_removed"] == ["b"] and d["nodes_added"] == ["c"]
        assert d["edges_removed"] and not d["edges_added"]


class TestWorkflowAssetStore:
    def test_save_versions_and_active(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        r1 = s.save("wf_a", _wf_body("v1"))
        assert r1["version"] == 1 and r1["status"] == "active"
        r2 = s.save("wf_a", _wf_body("v2"))
        assert r2["version"] == 2
        assert s.current_version("wf_a") == 2
        active = s.get_active("wf_a")
        assert active["workflow"]["nodes"][0]["config"]["params"]["say"] == "v2"
        assert s.get_version("wf_a", 1)["digest"] != r2["digest"]

    def test_rejected_version_does_not_activate(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        s.save("wf_b", _wf_body("ok"))
        bad = _wf_body("bad")
        bad["acceptance"] = [{"type": "forbidden_nodes", "names": ["echo_516"]}]
        rec = s.save("wf_b", bad)
        assert rec["status"] == "rejected"
        assert s.current_version("wf_b") == 1, "previous active version must stay current"

    def test_audit_trail_independent_of_logs(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        s.save("wf_c", _wf_body("v1"), actor="alice")
        s.save("wf_c", _wf_body("v2"), actor="bob")
        audit = s.audit("wf_c")
        assert len(audit) == 2
        assert audit[0]["actor"] == "alice" and audit[1]["actor"] == "bob"
        assert audit[0]["changed"] == {"initial": True}
        assert audit[1]["changed"]["nodes_changed"] == ["n1"] or audit[1]["changed"]["top_level_changed"]
        assert audit[1]["prev_digest"] == audit[0]["digest"]

    def test_diff_query(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        s.save("wf_d", _wf_body("v1"))
        s.save("wf_d", _wf_body("v2"))
        d = s.diff("wf_d", 1, 2)
        assert d["nodes_changed"] == ["n1"]
        assert s.diff("wf_d", 1, 99) is None

    def test_regression_gate_accept_and_reject(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        s.save("wf_e", _wf_body("hello"))
        s.record_golden_case("wf_e", {"echo": "hello"}, {"n1": canonical_digest({"echo": "hello"})})
        good = _wf_body("hello")
        good["regression"] = {"enabled": True}
        rec = s.save("wf_e", good, replay_fn=lambda wf, inputs: {"n1": canonical_digest({"echo": "hello"})})
        assert rec["status"] == "active" and rec["gate_results"][0]["ok"]

        bad = _wf_body("changed")
        bad["regression"] = {"enabled": True}
        rec = s.save(
            "wf_e", bad, replay_fn=lambda wf, inputs: {"n1": canonical_digest({"echo": "changed-but-different"})}
        )
        assert rec["status"] == "rejected"
        assert "output mismatch" in rec["gate_results"][-1]["detail"]
        assert s.current_version("wf_e") == 2, "rejected candidate must not become active"

    def test_regression_requires_golden_cases(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        body = _wf_body("x")
        body["regression"] = {"enabled": True}
        rec = s.save("wf_f", body, replay_fn=lambda wf, inputs: {})
        assert rec["status"] == "rejected"
        assert "no golden cases" in rec["gate_results"][0]["detail"]

    def test_replay_error_rejects(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        s.save("wf_g", _wf_body("x"))
        s.record_golden_case("wf_g", {}, {"n1": "abc"})
        body = _wf_body("x")
        body["regression"] = {"enabled": True}
        rec = s.save("wf_g", body, replay_fn=lambda wf, inputs: (_ for _ in ()).throw(RuntimeError("boom")))
        assert rec["status"] == "rejected" and "boom" in rec["gate_results"][-1]["detail"]

    def test_golden_suite_capped_at_10(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        for i in range(12):
            s.record_golden_case("wf_h", {"i": i}, {"n1": str(i)})
        assert len(s.load_golden("wf_h")["cases"]) == 10

    def test_invalid_id_rejected(self, tmp_path):
        s = WorkflowAssetStore(root=tmp_path)
        with pytest.raises(ValueError):
            s.save("../evil", _wf_body("x"))


@pytest.fixture
def client_516(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-516")
    eng = WorkflowEngine()
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    return TestClient(app), eng, tmp_path / "store" / "assets"


AUTH = {"Authorization": "Bearer test-key-516"}


class TestWebAssetRoutes:
    def test_create_stamps_version_and_routes(self, client_516):
        client, _, assets_root = client_516
        resp = client.post("/api/workflows", json=_wf_body("hello"), headers=AUTH)
        assert resp.status_code == 201, resp.text
        rec = resp.json()
        assert rec["workflow_version"] == 1
        assert len(rec["workflow_digest"]) == 16
        versions = client.get(f"/api/workflows/{rec['id']}/versions", headers=AUTH).json()
        assert versions["count"] == 1 and versions["versions"][0]["status"] == "active"

    def test_put_creates_new_version(self, client_516):
        client, _, _ = client_516
        wf_id = client.post("/api/workflows", json=_wf_body("v1"), headers=AUTH).json()["id"]
        resp = client.put(f"/api/workflows/{wf_id}", json=_wf_body("v2"), headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["workflow_version"] == 2
        versions = client.get(f"/api/workflows/{wf_id}/versions", headers=AUTH).json()
        assert versions["count"] == 2

    def test_acceptance_reject_returns_409_and_keeps_current(self, client_516):
        client, _, _ = client_516
        wf_id = client.post("/api/workflows", json=_wf_body("v1"), headers=AUTH).json()["id"]
        bad = _wf_body("v2")
        bad["acceptance"] = [{"type": "required_nodes", "names": ["must_exist"]}]
        resp = client.put(f"/api/workflows/{wf_id}", json=bad, headers=AUTH)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "asset_gate_rejected"
        assert detail["gate_results"][0]["ok"] is False
        current = client.get(f"/api/workflows/{wf_id}", headers=AUTH).json()
        assert current["workflow"]["nodes"][0]["config"]["params"]["say"] == "v1"
        versions = client.get(f"/api/workflows/{wf_id}/versions", headers=AUTH).json()
        assert versions["versions"][-1]["status"] == "rejected"

    def test_version_detail_diff_audit(self, client_516):
        client, _, _ = client_516
        wf_id = client.post("/api/workflows", json=_wf_body("v1"), headers=AUTH).json()["id"]
        client.put(f"/api/workflows/{wf_id}", json=_wf_body("v2"), headers=AUTH)
        detail = client.get(f"/api/workflows/{wf_id}/versions/2", headers=AUTH).json()
        assert detail["version"] == 2 and detail["workflow"]["nodes"][0]["config"]["params"]["say"] == "v2"
        diff = client.get(f"/api/workflows/{wf_id}/versions/diff", params={"v1": 1, "v2": 2}, headers=AUTH).json()
        assert diff["diff"]["nodes_changed"] == ["n1"]
        audit = client.get(f"/api/workflows/{wf_id}/audit", headers=AUTH).json()
        assert audit["count"] == 2
        assert audit["audit"][1]["prev_digest"] == audit["audit"][0]["digest"]
        missing = client.get(f"/api/workflows/{wf_id}/versions/9", headers=AUTH)
        assert missing.status_code == 404

    def test_regression_gate_via_web_replay(self, client_516):
        client, _, _ = client_516
        wf_id = client.post("/api/workflows", json=_wf_body("hello"), headers=AUTH).json()["id"]
        client.put(f"/api/workflows/{wf_id}", json=_wf_body("hello-2"), headers=AUTH)
        # candidate identical to golden params replays to matching digests
        good = _wf_body("hello-2")
        good["regression"] = {"enabled": True}
        resp = client.put(f"/api/workflows/{wf_id}", json=good, headers=AUTH)
        # no golden cases recorded yet -> regression enabled must reject
        assert resp.status_code == 409
        assert "no golden cases" in resp.json()["detail"]["gate_results"][0]["detail"]

    def test_run_records_asset_version(self, client_516):
        client, eng, assets_root = client_516
        wf_id = client.post("/api/workflows", json=_wf_body("hello"), headers=AUTH).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=AUTH).json()["run_id"]
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=AUTH).json()
            if detail.get("status") not in ("running", "queued"):
                break
            time.sleep(0.05)
        assert detail["status"] in ("success", "completed")
        assert detail["workflow_version"] == 1
        assert len(detail["workflow_digest"]) == 16
        # successful run becomes a golden case for the asset
        suite = json.loads((assets_root / wf_id / "golden.json").read_text(encoding="utf-8"))
        assert suite["cases"], "successful run must be recorded as golden case"
        assert "n1" in suite["cases"][-1]["node_digests"]
