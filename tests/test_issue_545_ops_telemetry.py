"""issue #545: DAG run/node telemetry into fusion_ops.ops_events (flywheel Loop D).

验收: 失败节点产生稳定 failure signature (跨 run 相同 cluster key);
payload 不含节点输入/输出 (tenant data 红线, 只有 workflow 定义 sha256);
telemetry 关闭 (无 FUSION_OPS_DSN) = 引擎行为零变化; emit 永不阻塞/抛错。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from fusion_harness import HumanGate

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.observability.ops_telemetry import OpsTelemetry, failure_signature, workflow_definition_hash

FAIL_NODE = "fail_probe_545"
OK_NODE = "ok_probe_545"


class _FakeEmitter:
    # captures events without PG; emit is async and never raises
    def __init__(self):
        self.events = []
        self.fail_mode = False

    async def emit(self, event: dict) -> bool:
        if self.fail_mode:
            raise RuntimeError("boom")
        self.events.append(event)
        return True


@register_node
class _FailProbe(BaseNode):
    name = FAIL_NODE
    display_name = "Fail Probe 545"
    category = NodeCategory.TOOL
    description = "test-only failing probe (#545)"
    icon = "💥"
    default_label = "Fail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="secret tenant path /Users/x/data.csv missing", summary="no")


@register_node
class _OkProbe(BaseNode):
    name = OK_NODE
    display_name = "Ok Probe 545"
    category = NodeCategory.TOOL
    description = "test-only ok probe (#545)"
    icon = "✅"
    default_label = "Ok"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"tenant_secret": "s3cret"}, summary="done")


@pytest.fixture
def pm(monkeypatch):
    # probes stay LOW-risk: AUTO + high-risk = headless deny-fast (deny), and
    # these tests exercise telemetry, not the HITL gate
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", "0")
    return PermissionManager(level=PermissionLevel.AUTO)


def _make_engine(pm, emitter):
    return WorkflowEngine(permission_manager=pm, ops_telemetry=OpsTelemetry(emitter=emitter))


class TestUnitHelpers:
    def _canonical(self, wf):
        nodes = wf.nodes.values() if isinstance(wf.nodes, dict) else wf.nodes
        id_to_name = {n.id: n.name for n in nodes}
        return {
            "name": wf.name,
            "nodes": sorted(id_to_name.values()),
            "edges": sorted(f"{id_to_name[e.source_id]}->{id_to_name[e.target_id]}" for e in wf.edges),
        }

    def test_workflow_definition_hash_is_sha256_prefix(self, pm):
        wf = Workflow(name="wf-545")
        wf.add_node(NodeRegistry.create(OK_NODE))
        h = workflow_definition_hash(wf)
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 16
        raw = json.dumps(self._canonical(wf), sort_keys=True, ensure_ascii=False)
        assert h == "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def test_workflow_definition_hash_stable_across_instances(self, pm):
        wf1 = Workflow(name="wf-545")
        wf1.add_node(NodeRegistry.create(OK_NODE))
        wf2 = Workflow(name="wf-545")
        wf2.add_node(NodeRegistry.create(OK_NODE))
        assert workflow_definition_hash(wf1) == workflow_definition_hash(wf2)

    def test_workflow_definition_hash_changed_definition_differs(self, pm):
        wf1 = Workflow(name="wf-545")
        wf1.add_node(NodeRegistry.create(OK_NODE))
        wf2 = Workflow(name="wf-545")
        wf2.add_node(NodeRegistry.create(OK_NODE))
        wf2.add_node(NodeRegistry.create(OK_NODE))
        assert workflow_definition_hash(wf1) != workflow_definition_hash(wf2)

    def test_failure_signature_format_and_default(self):
        assert failure_signature(FAIL_NODE, "TimeoutError") == f"{FAIL_NODE}:TimeoutError"
        assert failure_signature(FAIL_NODE, "") == f"{FAIL_NODE}:Unknown"
        assert failure_signature(FAIL_NODE, "BusinessFailure") == f"{FAIL_NODE}:BusinessFailure"

    def test_from_env_none_without_dsn(self, monkeypatch):
        monkeypatch.delenv("FUSION_OPS_DSN", raising=False)
        assert OpsTelemetry.from_env() is None

    def test_from_env_emitter_with_dsn(self, monkeypatch):
        monkeypatch.setenv("FUSION_OPS_DSN", "postgresql://ops:pw@127.0.0.1:5432/ops")
        t = OpsTelemetry.from_env()
        assert t is not None
        assert t._emitter is not None
        assert t._emitter._source == "cowork"

    def test_from_env_none_when_fusion_core_missing(self, monkeypatch):
        monkeypatch.setenv("FUSION_OPS_DSN", "postgresql://ops:pw@127.0.0.1:5432/ops")
        import builtins

        real_import = builtins.__import__

        def _no_ops(name, *a, **kw):
            if name.startswith("fusion_core"):
                raise ImportError("no fusion-core")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_ops)
        assert OpsTelemetry.from_env() is None


class TestEngineEmission:
    async def _run_ok(self, pm, emitter) -> str:
        eng = _make_engine(pm, emitter)
        wf = Workflow(name="ok-545")
        wf.add_node(NodeRegistry.create(OK_NODE))
        execution = await eng.execute(wf, execution_id="run-545-ok")
        assert execution.status == WorkflowStatus.SUCCESS
        return "run-545-ok"

    async def _run_fail(self, pm, emitter) -> str:
        eng = _make_engine(pm, emitter)
        wf = Workflow(name="fail-545")
        wf.add_node(NodeRegistry.create(FAIL_NODE))
        execution = await eng.execute(wf, execution_id="run-545-fail")
        assert execution.status == WorkflowStatus.FAILED
        return "run-545-fail"

    async def test_run_lifecycle_events(self, pm):
        em = _FakeEmitter()
        await self._run_ok(pm, em)
        types = [e["event_type"] for e in em.events]
        assert types == ["dag_run_started", "dag_node_started", "dag_node_finished", "dag_run_finished"]

    async def test_started_event_payload(self, pm):
        em = _FakeEmitter()
        await self._run_ok(pm, em)
        started = em.events[0]
        assert started["payload"]["run_id"] == "run-545-ok"
        assert started["payload"]["workflow_hash"].startswith("sha256:")
        assert started["payload"]["node_count"] == 1
        assert "input_data" not in started["payload"] and "output_data" not in started["payload"]

    async def test_node_finished_success_event(self, pm):
        em = _FakeEmitter()
        await self._run_ok(pm, em)
        finished = next(e for e in em.events if e["event_type"] == "dag_node_finished")
        assert finished["status"] == "success"
        assert finished["payload"]["node_type"] == OK_NODE
        assert finished["payload"]["attempts"] == 1
        assert finished["payload"]["failure_signature"] == ""
        assert "input_data" not in finished["payload"] and "output_data" not in finished["payload"]

    async def test_stable_failure_signature_across_runs(self, pm):
        # acceptance: same failing node twice = identical cluster key, no message text
        em = _FakeEmitter()
        await self._run_fail(pm, em)
        await self._run_fail(pm, em)
        sigs = [e["payload"]["failure_signature"] for e in em.events if e["event_type"] == "dag_node_finished"]
        assert len(sigs) == 2
        assert sigs[0] == sigs[1] == f"{FAIL_NODE}:BusinessFailure"
        # tenant data red line: error message text must not leak into any event
        blob = json.dumps(em.events, ensure_ascii=False)
        assert "tenant path" not in blob
        assert "/Users/x/data.csv" not in blob

    async def test_run_finished_carries_outcome_and_latency(self, pm):
        em = _FakeEmitter()
        await self._run_fail(pm, em)
        fin = em.events[-1]
        assert fin["event_type"] == "dag_run_finished"
        assert fin["status"] == "failed"
        assert isinstance(fin["latency_ms"], int) and fin["latency_ms"] >= 0
        assert fin["payload"]["workflow_hash"].startswith("sha256:")

    async def test_emit_failure_never_breaks_run(self, pm):
        em = _FakeEmitter()
        em.fail_mode = True
        eng = _make_engine(pm, em)
        wf = Workflow(name="boom-545")
        wf.add_node(NodeRegistry.create(OK_NODE))
        execution = await eng.execute(wf, execution_id="run-545-boom")
        assert execution.status == WorkflowStatus.SUCCESS

    async def test_telemetry_off_zero_events(self, pm):
        eng = WorkflowEngine(permission_manager=pm)
        wf = Workflow(name="off-545")
        wf.add_node(NodeRegistry.create(OK_NODE))
        execution = await eng.execute(wf, execution_id="run-545-off")
        assert execution.status == WorkflowStatus.SUCCESS
        assert eng._ops_telemetry is None


class TestWebAppWiring:
    async def test_create_web_app_builds_telemetry_from_env(self, pm, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_OPS_DSN", "postgresql://ops:pw@127.0.0.1:5432/ops")
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        from fusion_cowork.server.web_api import create_web_app

        app = create_web_app(engine=WorkflowEngine(permission_manager=pm))
        telemetry = getattr(app.state, "ops_telemetry", None)
        assert telemetry is not None, "FUSION_OPS_DSN set = telemetry wired"

    async def test_create_web_app_no_telemetry_without_env(self, pm, monkeypatch):
        monkeypatch.delenv("FUSION_OPS_DSN", raising=False)
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        from fusion_cowork.server.web_api import create_web_app

        app = create_web_app(engine=WorkflowEngine(permission_manager=pm))
        assert getattr(app.state, "ops_telemetry", None) is None

    async def test_start_stop_lifecycle_noop_without_emitter(self, pm, monkeypatch):
        monkeypatch.delenv("FUSION_OPS_DSN", raising=False)
        t = OpsTelemetry()
        await t.start()
        await t.stop()
        assert t.stats == {}

    async def test_stats_proxy(self):
        em = _FakeEmitter()
        em.stats = SimpleNamespace(emitted=3, flushed=2, dropped=1, reconnects=0)
        t = OpsTelemetry(emitter=em)
        await t._emit({"event_type": "dag_run_started", "payload": {}})
        assert len(em.events) == 1
        assert t.stats == {"emitted": 3, "flushed": 2, "dropped": 1, "reconnects": 0}


class TestBackpressure:
    async def test_hitl_run_with_telemetry_no_deadlock(self, pm, monkeypatch, tmp_path):
        # telemetry emits inside the HITL wait loop path — full park/resume cycle
        # must not deadlock or block on emit (backpressure contract)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        from fusion_cowork.engine import permission_manager

        monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {OK_NODE})
        em = _FakeEmitter()
        gate = HumanGate(str(tmp_path / "gate-545"))
        eng = WorkflowEngine(permission_manager=pm, hitl_gate=gate, ops_telemetry=OpsTelemetry(emitter=em))
        wf = Workflow(name="hitl-545")
        wf.add_node(NodeRegistry.create(OK_NODE))
        task = asyncio.create_task(eng.execute(wf, execution_id="run-545-hitl"))
        for _ in range(250):
            if eng.hitl_pending("run-545-hitl"):
                break
            await asyncio.sleep(0.02)
        assert eng.resolve_hitl("run-545-hitl", True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        types = [e["event_type"] for e in em.events]
        assert "dag_run_started" in types and "dag_run_finished" in types
