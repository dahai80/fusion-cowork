# issue #650: per-stage evidence artifacts — content-addressed store with sha256
# fingerprints, append-only sealed stages, gate-transition validation, and the
# run evidence API (list + raw byte-identical fetch with hash re-verification).

import asyncio
import hashlib
import json

import pytest

from fusion_cowork.engine.evidence import (
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    EvidenceSealedError,
    EvidenceStore,
    canonical_output_bytes,
)
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine


class MockSuccessNode(BaseNode):
    name = "mock_success"
    display_name = "Mock success node"
    category = NodeCategory.TOOL
    description = "test: always succeeds"
    icon = "T"
    default_label = "Mock success"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": "ok", "input": inputs}, summary="ok")


class MockFailNode(BaseNode):
    name = "mock_fail"
    display_name = "Mock fail node"
    category = NodeCategory.TOOL
    description = "test: always fails"
    icon = "T"
    default_label = "Mock fail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="business failure")


@pytest.fixture
def store(tmp_path):
    return EvidenceStore(tmp_path / "evidence")


def _run(engine, wf):
    return asyncio.run(engine.execute(wf))


class TestEvidenceStore:
    def test_fingerprint_stable_across_duplicate_content(self, store):
        content = b"identical payload"
        r1 = store.append("run1", "n1", "node_output", content)
        r2 = store.append("run1", "n2", "node_output", content)
        assert r1["fingerprint"] == r2["fingerprint"] == hashlib.sha256(content).hexdigest()

    def test_canonical_output_bytes_deterministic(self):
        a = canonical_output_bytes({"b": 1, "a": [1, 2]})
        b = canonical_output_bytes({"a": [1, 2], "b": 1})
        assert a == b

    def test_append_after_seal_rejected(self, store):
        store.append("run1", "n1", "node_output", b"x")
        store.seal_stage("run1", "n1")
        with pytest.raises(EvidenceSealedError):
            store.append("run1", "n1", "node_output", b"mutated")

    def test_seal_idempotent(self, store):
        store.seal_stage("run1", "n1")
        store.seal_stage("run1", "n1")
        assert store.is_sealed("run1", "n1")

    def test_sealed_state_survives_restart(self, tmp_path):
        root = tmp_path / "evidence"
        s1 = EvidenceStore(root)
        s1.seal_stage("run1", "n1")
        s2 = EvidenceStore(root)
        assert s2.is_sealed("run1", "n1")
        with pytest.raises(EvidenceSealedError):
            s2.append("run1", "n1", "node_output", b"x")

    def test_raw_fetch_byte_identical_and_reverified(self, store):
        content = json.dumps({"k": "值", "n": 42}, ensure_ascii=False).encode("utf-8")
        rec = store.append("run1", "n1", "node_output", content)
        assert store.read_raw(rec["evidence_id"]) == content
        blob = store._blob_path(rec["fingerprint"])
        blob.write_bytes(b"tampered")
        with pytest.raises(EvidenceIntegrityError):
            store.read_raw(rec["evidence_id"])

    def test_unknown_evidence_not_found(self, store):
        with pytest.raises(EvidenceNotFoundError):
            store.read_raw("ev_missing")

    def test_append_requires_bytes_and_ids(self, store):
        with pytest.raises(TypeError):
            store.append("run1", "n1", "node_output", "not-bytes")
        with pytest.raises(ValueError):
            store.append("", "n1", "node_output", b"x")

    def test_list_for_run_filters_by_run(self, store):
        store.append("run1", "n1", "node_output", b"a")
        store.append("run2", "n1", "node_output", b"b")
        assert [r["run_id"] for r in store.list_for_run("run1")] == ["run1"]


class TestEngineEvidenceWiring:
    def test_success_node_records_evidence_and_seals(self, store):
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, evidence_store=store)
        wf = Workflow(name="wf650")
        wf.add_node(MockSuccessNode(node_id="a"))
        _run(eng, wf)
        exec_id = list(eng._executions.keys())[-1]
        recs = store.list_for_run(exec_id)
        assert len(recs) == 1
        rec = recs[0]
        assert rec["node_id"] == "a"
        assert rec["artifact_type"] == "node_output"
        expected = hashlib.sha256(canonical_output_bytes({"result": "ok", "input": {"run_id": exec_id}})).hexdigest()
        assert rec["fingerprint"] == expected
        assert store.is_sealed(exec_id, "a")

    def test_failed_node_seals_without_evidence(self, store):
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, evidence_store=store)
        wf = Workflow(name="wf650f")
        node = MockFailNode(node_id="a")
        node.config.continue_on_error = True
        wf.add_node(node)
        _run(eng, wf)
        exec_id = list(eng._executions.keys())[-1]
        assert store.list_for_run(exec_id) == []
        assert store.is_sealed(exec_id, "a")

    def test_gate_without_upstream_evidence_rejected(self, store):
        from fusion_cowork.nodes.ecosystem.decision_gate_node import DecisionGateNode

        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, evidence_store=store)
        wf = Workflow(name="wf650g")
        node = MockFailNode(node_id="a")
        node.config.continue_on_error = True
        gate = DecisionGateNode(node_id="gate")
        gate.config.continue_on_error = False
        wf.add_node(node)
        wf.add_node(gate)
        wf.connect("a", "gate")
        execution = _run(eng, wf)
        failed = [s for s in execution.steps if s.node_id == "gate"]
        assert failed and failed[0].status == NodeStatus.FAILED
        assert "no sealed evidence" in (failed[0].error or "")
        assert "'a'" in (failed[0].error or "")

    def test_gate_with_upstream_evidence_not_boundary_rejected(self, store):
        from fusion_cowork.nodes.ecosystem.decision_gate_node import DecisionGateNode

        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, evidence_store=store)
        wf = Workflow(name="wf650g2")
        wf.add_node(MockSuccessNode(node_id="a"))
        gate = DecisionGateNode(node_id="gate")
        gate.config.continue_on_error = True
        wf.add_node(gate)
        wf.connect("a", "gate")
        execution = _run(eng, wf)
        gate_steps = [s for s in execution.steps if s.node_id == "gate"]
        assert gate_steps
        assert "no sealed evidence" not in (gate_steps[-1].error or "")

    def test_no_store_zero_behavior(self):
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        wf = Workflow(name="wf650n")
        wf.add_node(MockSuccessNode(node_id="a"))
        execution = _run(eng, wf)
        from fusion_cowork.engine.workflow import WorkflowStatus

        assert execution.status == WorkflowStatus.SUCCESS


STATIC_KEY = "fc-web-test-650"


class TestEvidenceAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        from fusion_cowork.server import web_api

        web_api._WEB_DIR = tmp_path / "web"
        eng = WorkflowEngine(
            permission_manager=PermissionManager(),
            hitl_pause=False,
            evidence_store=EvidenceStore(tmp_path / "evidence"),
        )
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": f"Bearer {STATIC_KEY}"}
        with TestClient(app) as client:
            yield client, app, auth

    def _seed_run(self, app, run_id):
        app.state.run_registry._runs[run_id] = {"run_id": run_id, "workflow_name": "wf", "status": "success"}

    def test_evidence_list_groups_by_stage(self, api):
        client, app, auth = api
        store = app.state.run_registry._engine.evidence_store
        rec = store.append("run-650", "stage_a", "node_output", b"payload-650", meta={"node_name": "mock"})
        store.seal_stage("run-650", "stage_a")
        self._seed_run(app, "run-650")
        r = client.get("/api/runs/run-650/evidence", headers=auth)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["count"] == 1
        stage = data["stages"][0]
        assert stage["node_id"] == "stage_a"
        assert stage["node_name"] == "mock"
        assert stage["sealed"] is True
        assert stage["evidence"][0]["fingerprint"] == rec["fingerprint"]
        assert stage["evidence"][0]["evidence_id"] == rec["evidence_id"]

    def test_evidence_raw_byte_identical(self, api):
        client, app, auth = api
        store = app.state.run_registry._engine.evidence_store
        rec = store.append("run-650r", "stage_a", "node_output", b"payload-650")
        self._seed_run(app, "run-650r")
        raw = client.get(f"/api/runs/run-650r/evidence/{rec['evidence_id']}/raw", headers=auth)
        assert raw.status_code == 200
        assert raw.content == b"payload-650"
        assert raw.headers["X-Evidence-Fingerprint"] == rec["fingerprint"]
        assert raw.headers["X-Evidence-Type"] == "node_output"

    def test_evidence_raw_integrity_409(self, api):
        client, app, auth = api
        store = app.state.run_registry._engine.evidence_store
        rec = store.append("run-650i", "stage_a", "node_output", b"payload")
        self._seed_run(app, "run-650i")
        store._blob_path(rec["fingerprint"]).write_bytes(b"corrupted")
        r = client.get(f"/api/runs/run-650i/evidence/{rec['evidence_id']}/raw", headers=auth)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "integrity_violation"

    def test_evidence_run_404(self, api):
        client, app, auth = api
        assert client.get("/api/runs/nope/evidence", headers=auth).status_code == 404

    def test_evidence_unknown_id_404(self, api):
        client, app, auth = api
        self._seed_run(app, "run-650u")
        r = client.get("/api/runs/run-650u/evidence/ev_missing/raw", headers=auth)
        assert r.status_code == 404

    def test_evidence_cross_run_raw_404(self, api):
        client, app, auth = api
        store = app.state.run_registry._engine.evidence_store
        rec = store.append("run-x", "n", "node_output", b"secret")
        self._seed_run(app, "run-y")
        r = client.get(f"/api/runs/run-y/evidence/{rec['evidence_id']}/raw", headers=auth)
        assert r.status_code == 404

    def test_evidence_requires_auth(self, api):
        client, app, auth = api
        self._seed_run(app, "run-650a")
        assert client.get("/api/runs/run-650a/evidence").status_code == 401
