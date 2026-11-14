"""issue #470: DAG node state bound to harness kernel primitives + workflow ladder node.

- KernelCheckpointBinder: pre-execution checkpoints into harness CheckpointStore
  (session_id = run_id, metadata carries node_id/node_name/params)
- ReceiptCache: committed nodes idempotent — same execution_id re-dispatch
  replays the stored output (SKIPPED step) instead of re-executing effects
- WorkflowEngine(kernel_checkpoints=...) opt-in seam; None default = zero change
- workflow_ladder node: staged workers via kernel ParallelTeam, stage summaries
  feed the next stage (ladder propagation), failure propagation, allow_partial,
  harness-missing loud error, HIGH_RISK gate, Chinese aliases
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fusion_cowork.engine import PermissionManager
from fusion_cowork.engine.kernel_checkpoint import KernelCheckpointBinder
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

ECHO_470 = "ladder_echo_470"
FAIL_470 = "ladder_fail_470"


@register_node
class _EchoProbe(BaseNode):
    name = ECHO_470
    display_name = "Ladder Echo 470"
    category = NodeCategory.LOGIC
    description = "测试用: 回显 probe (#470)"
    icon = "🧪"
    default_label = "Echo470"

    async def execute(self, inputs: dict) -> NodeResult:
        prompt = str(inputs.get("prompt", ""))
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"prompt": prompt},
            summary=f"echo:{prompt[-120:]}",
        )


@register_node
class _FailProbe(BaseNode):
    name = FAIL_470
    display_name = "Ladder Fail 470"
    category = NodeCategory.LOGIC
    description = "测试用: 失败 probe (#470)"
    icon = "🧪"
    default_label = "Fail470"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="probe 470 intentional failure")


@register_node
class _CountProbe(BaseNode):
    name = "ladder_count_470"
    display_name = "Ladder Count 470"
    category = NodeCategory.LOGIC
    description = "测试用: 执行计数 probe (#470)"
    icon = "🧪"
    default_label = "Count470"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        _CountProbe.calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"calls": _CountProbe.calls}, summary="counted")


@register_node
class _SlowProbe(BaseNode):
    name = "ladder_slow_470"
    display_name = "Ladder Slow 470"
    category = NodeCategory.LOGIC
    description = "测试用: 慢 probe (#470 超时)"
    icon = "🧪"
    default_label = "Slow470"

    async def execute(self, inputs: dict) -> NodeResult:
        await asyncio.sleep(2)
        return NodeResult(status=NodeStatus.SUCCESS, data={}, summary="slow done")


@pytest.fixture(autouse=True)
def _probes():
    for probe in (ECHO_470, FAIL_470):
        if NodeRegistry.get(probe) is None:
            import fusion_cowork.nodes.ecosystem.ladder_node  # noqa: F401
    yield


def _make(name: str, params: dict) -> BaseNode:
    node_cls = NodeRegistry.get(name)
    return node_cls(config=NodeConfig(params=params))


class TestKernelCheckpointBinder:
    def test_import_error_loud(self, monkeypatch):
        import builtins
        import sys

        real_import = builtins.__import__

        def _no_harness(name, *a, **kw):
            if name == "fusion_harness" or name.startswith("fusion_harness."):
                raise ImportError("No module named 'fusion_harness'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_harness)
        monkeypatch.setitem(sys.modules, "fusion_harness", None)
        with pytest.raises(ImportError, match="fusion-harness"):
            KernelCheckpointBinder()

    async def test_checkpoint_save_and_latest(self):
        binder = KernelCheckpointBinder()
        try:
            binder.save_node_checkpoint("run_ck_1", "n1", "shell_exec", {"cmd": "ls"}, {"data": 1})
            state = binder.latest_checkpoint("run_ck_1")
            assert state is not None
            assert state.metadata["node_id"] == "n1"
            assert state.metadata["node_name"] == "shell_exec"
            assert state.metadata["params"] == {"cmd": "ls"}
            assert state.permission_mode == "workflow"
            assert json.loads(state.messages[0]["content"]) == {"data": 1}
            assert len(binder.list_checkpoints("run_ck_1")) == 1
            # 第二个节点推进 — latest 跟随最新节点 (失败 run 定位点)
            binder.save_node_checkpoint("run_ck_1", "n2", "python_repl", {}, {})
            assert binder.latest_checkpoint("run_ck_1").metadata["node_id"] == "n2"
            assert binder.latest_checkpoint("run_ck_1").metadata["node_name"] == "python_repl"
            # 空 run 无检查点
            assert binder.latest_checkpoint("run_ck_none") is None
        finally:
            binder.close()

    async def test_receipt_record_replay(self):
        binder = KernelCheckpointBinder()
        try:
            assert binder.has_receipt("run_rc_1", "n1") is False
            assert binder.replay_receipt("run_rc_1", "n1") is None
            result, replayed = binder.record_receipt("run_rc_1", "n1", {"out": 42})
            assert replayed is False
            assert result == {"out": 42}
            assert binder.has_receipt("run_rc_1", "n1") is True
            replay = binder.replay_receipt("run_rc_1", "n1")
            assert replay == {"out": 42}
            # 重复记录同 key = 回放不覆盖 (first-wins 幂等)
            result2, replayed2 = binder.record_receipt("run_rc_1", "n1", {"out": "changed"})
            assert replayed2 is True
            assert result2 == {"out": 42}
            # 非 JSON 安全输出也落得进去 (default=str 兜底)
            import datetime

            binder.record_receipt("run_rc_1", "n2", {"ts": datetime.datetime(2026, 11, 14)})
            assert binder.replay_receipt("run_rc_1", "n2") is not None
        finally:
            binder.close()


class TestEngineCheckpointSeam:
    async def test_receipt_replay_skips_reexecution(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        binder = KernelCheckpointBinder()
        try:
            eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, kernel_checkpoints=binder)
            wf = Workflow(name="wf-470-replay")
            wf.add_node(NodeRegistry.create("ladder_count_470", node_id="n1"))
            _CountProbe.calls = 0
            ex1 = await eng.execute(wf, execution_id="run_470_replay")
            assert ex1.status.value == "success"
            assert _CountProbe.calls == 1
            # 同 execution_id 重派发 — 回执命中, 节点不重执行, 输出重放
            ex2 = await eng.execute(wf, execution_id="run_470_replay")
            assert ex2.status.value == "success"
            assert _CountProbe.calls == 1
            replay_step = ex2.steps[-1]
            assert replay_step.status.value == "skipped"
            assert "replayed from kernel checkpoint" in (replay_step.summary or "")
            assert replay_step.output_data == {"calls": 1}
            # 检查点逐节点落盘 (pre-state)
            cps = binder.list_checkpoints("run_470_replay")
            assert len(cps) == 1
        finally:
            binder.close()

    async def test_checkpoints_track_node_progress(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        binder = KernelCheckpointBinder()
        try:
            eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, kernel_checkpoints=binder)
            wf = Workflow(name="wf-470-ck")
            wf.add_node(NodeRegistry.create("ladder_count_470", node_id="na"))
            wf.add_node(NodeRegistry.create("ladder_count_470", node_id="nb"))
            wf.connect("na", "nb")
            ex = await eng.execute(wf, execution_id="run_470_ck")
            assert ex.status.value == "success"
            latest = binder.latest_checkpoint("run_470_ck")
            assert latest.metadata["node_id"] == "nb"
            assert len(binder.list_checkpoints("run_470_ck")) == 2
        finally:
            binder.close()

    async def test_no_binder_zero_behavior_change(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        assert eng._kernel_checkpoints is None
        wf = Workflow(name="wf-470-plain")
        wf.add_node(NodeRegistry.create("ladder_count_470", node_id="n1"))
        _CountProbe.calls = 0
        ex = await eng.execute(wf, execution_id="run_470_plain")
        assert ex.status.value == "success"
        assert _CountProbe.calls == 1


class TestWorkflowLadderNode:
    async def test_two_stage_ack_ladder(self):
        node = _make(
            "workflow_ladder",
            {
                "stages": [
                    {"name": "s1", "workers": [{"name": "a", "prompt": "p1"}, {"name": "b", "prompt": "p2"}]},
                    {"name": "s2", "workers": [{"name": "c", "prompt": "p3"}]},
                ]
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["total"] == 3
        assert res.data["failed"] == 0
        assert [s["name"] for s in res.data["stages"]] == ["s1", "s2"]
        assert all(r["status"] == "completed" for s in res.data["stages"] for r in s["results"])

    async def test_stage_context_propagates(self):
        node = _make(
            "workflow_ladder",
            {
                "stages": [
                    {"name": "s1", "workers": [{"name": "w", "prompt": "STAGE1-MARKER-470", "worker_node": ECHO_470}]},
                    {"name": "s2", "workers": [{"name": "w2", "prompt": "tail", "worker_node": ECHO_470}]},
                ]
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        s2_summary = res.data["stages"][1]["results"][0]["summary"]
        assert "Prior stage results (ladder context)" in s2_summary
        assert "STAGE1-MARKER-470" in s2_summary

    async def test_propagate_context_disabled(self):
        node = _make(
            "workflow_ladder",
            {
                "propagate_context": False,
                "stages": [
                    {"name": "s1", "workers": [{"name": "w", "prompt": "STAGE1-MARKER-470", "worker_node": ECHO_470}]},
                    {"name": "s2", "workers": [{"name": "w2", "prompt": "tail", "worker_node": ECHO_470}]},
                ],
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        s2_summary = res.data["stages"][1]["results"][0]["summary"]
        assert "Prior stage results" not in s2_summary

    async def test_worker_failure_propagates(self):
        node = _make(
            "workflow_ladder",
            {
                "stages": [
                    {"name": "s1", "workers": [{"name": "w", "prompt": "p", "worker_node": ECHO_470}]},
                    {"name": "s2", "workers": [{"name": "bad", "prompt": "p", "worker_node": FAIL_470}]},
                ]
            },
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "s2" in res.error
        assert "bad" in res.error
        # 失败阶段前的阶段结果保留在 data (部分可见)
        assert res.data["stages"][0]["name"] == "s1"

    async def test_allow_partial_continues(self):
        node = _make(
            "workflow_ladder",
            {
                "allow_partial": True,
                "stages": [
                    {"name": "s1", "workers": [{"name": "bad", "prompt": "p", "worker_node": FAIL_470}]},
                    {"name": "s2", "workers": [{"name": "ok", "prompt": "p", "worker_node": ECHO_470}]},
                ],
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["failed"] == 1
        assert res.error  # 失败计数诚实暴露

    async def test_missing_stages_param(self):
        node = _make("workflow_ladder", {})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "stages" in res.error

    async def test_stage_missing_workers(self):
        node = _make("workflow_ladder", {"stages": [{"name": "s1"}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "workers" in res.error

    async def test_worker_missing_prompt(self):
        node = _make("workflow_ladder", {"stages": [{"name": "s1", "workers": [{"name": "w"}]}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "prompt" in res.error

    async def test_stage_timeout(self):
        node = _make(
            "workflow_ladder",
            {
                "timeout": 0.1,
                "stages": [
                    {"name": "s1", "workers": [{"name": "slow", "prompt": "p", "worker_node": "ladder_slow_470"}]}
                ],
            },
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "超时" in res.error

    async def test_harness_missing_loud_error(self, monkeypatch):
        import builtins
        import sys

        real_import = builtins.__import__

        def _no_harness(name, *a, **kw):
            if name == "fusion_harness" or name.startswith("fusion_harness."):
                raise ImportError("No module named 'fusion_harness'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_harness)
        monkeypatch.setitem(sys.modules, "fusion_harness", None)
        node = _make("workflow_ladder", {"stages": [{"name": "s1", "workers": [{"name": "a", "prompt": "p"}]}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "fusion-harness" in res.error

    def test_high_risk_gate(self):
        assert "workflow_ladder" in HIGH_RISK_NODES

    def test_chinese_aliases(self):
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES.get("阶梯编排") == "workflow_ladder"
        assert NODE_NAME_ALIASES.get("工作流阶梯") == "workflow_ladder"
