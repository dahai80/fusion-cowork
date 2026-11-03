"""issue #260: HITL 闸门等待期 run 可见性 — blocked 步骤 + per-run hitl_pause 覆盖。"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine.node import NodeStatus
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


def _gated_wf() -> Workflow:
    wf = Workflow(name="wf-260")
    from fusion_cowork.engine.node import NodeRegistry

    wf.add_node(NodeRegistry.create("shell_exec", node_id="n1"))
    return wf


@pytest.mark.asyncio
async def test_gate_entry_writes_blocked_step(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
    task = asyncio.create_task(eng.execute(_gated_wf(), execution_id="run_gate"))
    blocked_seen = False
    for _ in range(200):
        ex = eng._executions.get("run_gate")
        if ex and any(s.status == NodeStatus.BLOCKED for s in ex.steps):
            blocked_seen = True
            break
        await asyncio.sleep(0.01)
    assert blocked_seen, "闸门等待期时间线应出现 blocked 步骤 (issue #260: running+0 steps 观感挂死)"
    assert eng.hitl_pending("run_gate")
    eng.resolve_hitl("run_gate", True)
    execution = await task
    statuses = [s.status for s in execution.steps]
    assert NodeStatus.BLOCKED in statuses
    assert execution.status.value != "running"
    assert "run_gate" not in eng._hitl_pause_overrides


@pytest.mark.asyncio
async def test_per_run_headless_override_denies_fast(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
    eng.set_run_hitl_pause("run_fast", False)
    execution = await eng.execute(_gated_wf(), execution_id="run_fast")
    assert execution.status.value in ("failed", "completed_with_denials")
    assert all(s.status != NodeStatus.BLOCKED for s in execution.steps)
    assert "run_fast" not in eng._hitl_pause_overrides


@pytest.mark.asyncio
async def test_per_run_pause_override_on_headless_engine(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", "0")
    eng = WorkflowEngine(permission_manager=PermissionManager())
    assert eng._hitl_pause is False
    eng.set_run_hitl_pause("run_pause", True)
    task = asyncio.create_task(eng.execute(_gated_wf(), execution_id="run_pause"))
    gated = False
    for _ in range(200):
        if eng.hitl_pending("run_pause"):
            gated = True
            break
        await asyncio.sleep(0.01)
    assert gated, "headless 引擎的 per-run 覆盖应生效进入审批等待"
    eng.resolve_hitl("run_pause", False)
    execution = await task
    assert execution.status.value in ("failed", "completed_with_denials")


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k260")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _wf_body() -> dict:
    return {
        "workflow": {
            "name": "wf260api",
            "nodes": [{"id": "n1", "name": "shell_exec", "config": {}}],
            "edges": [],
        }
    }


def test_api_hitl_pause_false_runs_fail_fast(monkeypatch, tmp_path):
    tc = _client(monkeypatch, tmp_path)
    with tc:
        body = _wf_body()
        body["hitl_pause"] = False
        r = tc.post("/api/runs", json=body, headers={"x-api-key": "k260"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        for _ in range(100):
            rec = tc.get(f"/api/runs/{run_id}", headers={"x-api-key": "k260"}).json()
            if rec["status"] not in ("running", "pending"):
                break
            time.sleep(0.05)
        assert rec["status"] in ("failed", "completed_with_denials")
        assert rec["steps"] and rec["steps"][-1]["status"] == "denied"


def test_api_hitl_pause_bad_type_400(monkeypatch, tmp_path):
    tc = _client(monkeypatch, tmp_path)
    with tc:
        body = _wf_body()
        body["hitl_pause"] = "yes"
        r = tc.post("/api/runs", json=body, headers={"x-api-key": "k260"})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "bad_request"
