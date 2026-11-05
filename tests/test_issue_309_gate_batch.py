"""issue #309: approval_gate batch semantics (per-item accept/reject).

Decision payloads may carry items[] ({task_issue, approved, reason?}): all
approved = flat behavior; mixed = run continues past the gate with the full
verdict injected into the gated node's input as batch_verdict (partial
completion tracked); zero approved = deny. The webhook route computes the
resume flag, persists the verdict into the run record (batch_verdicts) and
best-effort writes state:blocked back to GitHub for rejected items.
"""

import asyncio
import hashlib
import hmac
import json

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeConfig, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Edge, Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


@pytest.fixture(autouse=True)
def _hitl_on(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@register_node
class Probe309(BaseNode):
    name = "probe_309"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"seen": inputs.get("batch_verdict") or []}, summary="ok")


def _gate_workflow():
    wf = Workflow(name="wf309")
    wf.add_node(NodeRegistry.create("approval_gate", node_id="g1", config=NodeConfig()))
    wf.add_node(NodeRegistry.create("probe_309", node_id="p1", config=NodeConfig()))
    wf.edges.append(Edge(source_id="g1", target_id="p1"))
    return wf


def _items(*pairs):
    return [{"task_issue": n, "approved": a} for n, a in pairs]


class TestEngineBatch:
    @pytest.mark.asyncio
    async def test_mixed_verdict_flows_into_gate_output_and_downstream(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        items = _items((101, True), (102, False), (103, True))

        async def resolver():
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    exec_id = (pm.get_pending_details(action_id) or {}).get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(exec_id, True, once=True, batch=items, attribution={"decided_by": "webhook"})

        task = asyncio.create_task(resolver())
        try:
            execution = await asyncio.wait_for(eng.execute(_gate_workflow(), execution_id="run_309_mixed"), timeout=10)
        finally:
            task.cancel()
        assert execution.status.value == "success"
        gate_out = next(s for s in execution.steps if s.node_id == "g1" and s.status == NodeStatus.SUCCESS)
        assert gate_out.output_data["batch_verdict"] == items
        assert gate_out.output_data["rejected_items"] == [_items((102, False))[0]]
        assert f"{len(items) - 1}/{len(items)}" in gate_out.summary
        probe_out = next(s for s in execution.steps if s.node_id == "p1")
        assert probe_out.output_data["seen"] == items

    @pytest.mark.asyncio
    async def test_zero_approved_denies_run(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)

        async def resolver():
            while True:
                await asyncio.sleep(0.02)
                for action_id in list(pm._pending_local_approvals.keys()):
                    exec_id = (pm.get_pending_details(action_id) or {}).get("execution_id", "")
                    if not exec_id:
                        continue
                    pm.release_pending_once(action_id)
                    eng.resolve_hitl(exec_id, False, once=True, batch=_items((101, False)))

        task = asyncio.create_task(resolver())
        try:
            execution = await asyncio.wait_for(eng.execute(_gate_workflow(), execution_id="run_309_allno"), timeout=10)
        finally:
            task.cancel()
        assert execution.status.value == "failed"

    def test_resolve_hitl_ignores_non_list_batch(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm)
        assert eng.resolve_hitl("run_x", True, batch="nope") is False  # no pause event anyway


class TestWebhookItems:
    def _post(self, tc, run_id, payload):
        secret = "s309"
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return tc.post(f"/api/hooks/webhook/{run_id}", content=body, headers={"X-Fusion-Signature": sig})

    def _app_with_pending(self, monkeypatch, tmp_path, run_id):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEBHOOK_SECRET", "s309")
        app = web_api.create_web_app(
            dist_dir=tmp_path / "nd", store_root=tmp_path / "wf", space_data_dir=tmp_path / "sp"
        )
        pm = app.state.permission_manager
        pm._pending_local_approvals[f"act_{run_id}"] = "approval_gate"
        pm._pending_local_details[f"act_{run_id}"] = {
            "tool_name": "approval_gate",
            "execution_id": run_id,
            "node_id": "g1",
        }
        return TestClient(app)

    def test_items_echo_and_flag_computation(self, monkeypatch, tmp_path):
        tc = self._app_with_pending(monkeypatch, tmp_path, "run_309_webhook")
        with tc:
            r = self._post(
                tc,
                "run_309_webhook",
                {
                    "approved": False,
                    "items": [
                        {"task_issue": 101, "approved": True},
                        {"task_issue": 102, "approved": False, "reason": "no evidence"},
                    ],
                },
            )
            assert r.status_code == 200
            data = r.json()
            assert data["items"] == 2
            # mixed -> run continues, resume flag reflects the any-approved rule
            assert data["approved"] is True

    def test_zero_approved_items_flag_false(self, monkeypatch, tmp_path):
        tc = self._app_with_pending(monkeypatch, tmp_path, "run_309_webhook2")
        with tc:
            r = self._post(
                tc,
                "run_309_webhook2",
                {
                    "approved": True,
                    "items": [
                        {"task_issue": 101, "approved": False},
                        {"task_issue": 102, "approved": False},
                    ],
                },
            )
            assert r.json()["approved"] is False

    def test_non_dict_items_ignored(self, monkeypatch, tmp_path):
        tc = self._app_with_pending(monkeypatch, tmp_path, "run_309_webhook3")
        with tc:
            r = self._post(
                tc, "run_309_webhook3", {"approved": True, "items": ["nope", {"task_issue": 9, "approved": True}]}
            )
            assert r.json()["items"] == 1

    def test_rejected_items_state_write_without_token_skips(self, monkeypatch, tmp_path):
        # no GITHUB_TOKEN -> write path skipped cleanly, verdict still processed
        tc = self._app_with_pending(monkeypatch, tmp_path, "run_309_webhook4")
        with tc:
            r = self._post(
                tc,
                "run_309_webhook4",
                {
                    "approved": True,
                    "repo": "org/x",
                    "items": [
                        {"task_issue": 101, "approved": True},
                        {"task_issue": 102, "approved": False, "reason": "r"},
                    ],
                },
            )
            assert r.status_code == 200
            assert r.json()["approved"] is True
