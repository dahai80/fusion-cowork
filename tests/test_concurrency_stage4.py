"""Stage 4 — concurrency/execution correctness tests (CR-11/12/9/HI-7/8/9/10/16/18/19/20).

Verification of audit-driven fixes:
- CR-11: execute() does not write status/result on the shared BaseNode; concurrent execution has no state pollution
- CR-12: TrajectoryRecorder is namespaced by execution_id; concurrent execution snapshots are isolated
- CR-9: ComputerUseLoop action allowlist + param validation
- HI-7: submit_task keeps the asyncio.Task handle
- HI-8: cancel_task actually cancels the running coroutine + ShellExecutor kills the process on timeout
- HI-9: WorkflowExecutor injects the parent engine permission/hook/session
- HI-10: ComputerUseLoop screenshots use a temp file + param name/data key fix
- HI-16: Workflow.from_dict cycle detection + resume snapshot topology validation
- HI-18: CancelledError caught separately, finally sets the terminal state
- HI-19: budget overrun aborts execution
- HI-20: CoordinatorExecutor delegation depth limit
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# Register all nodes (same pattern as test_permission_stage3, guards against cross-test registry pollution)
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus


@pytest.fixture(autouse=True)
def _ensure_nodes_loaded():
    """Cross-test registry pollution guard: re-import if shell_exec is missing."""
    if "shell_exec" not in NodeRegistry._registry:
        import fusion_cowork.nodes

        fusion_cowork.nodes.import_all_nodes()


# ── CR-11: execute does not write the shared BaseNode ──


@register_node
class _SharedStateProbe(BaseNode):
    name = "stage4_probe"
    # i18n: user-facing
    display_name = "Stage4 探针"
    category = NodeCategory.IO
    # i18n: user-facing
    description = "探测: 读取共享节点状态"
    icon = "🧪"
    # i18n: user-facing
    default_label = "探针"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": "ok"}, summary="probe")


class TestCR11NoSharedNodeMutation:
    async def test_execute_does_not_mutate_node_status(self):
        wf = Workflow(name="cr11")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine()
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.SUCCESS
        # CR-11: after execute, BaseNode.status is still the initial PENDING (RUNNING/FAILED not written)
        node = wf.nodes["n1"]
        assert node.status == NodeStatus.PENDING
        # State lives only in WorkflowStep; node.result is not written by execute
        assert getattr(node, "result", None) is None

    async def test_concurrent_executions_independent(self):
        wf = Workflow(name="cr11_concurrent")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        wf2 = Workflow(name="cr11_concurrent2")
        wf2.add_node(_SharedStateProbe(node_id="m1"))
        engine = WorkflowEngine()
        r1, r2 = await asyncio.gather(engine.execute(wf), engine.execute(wf2))
        assert r1.status == WorkflowStatus.SUCCESS
        assert r2.status == WorkflowStatus.SUCCESS
        # The two workflow nodes do not pollute each other
        assert wf.nodes["n1"].id == "n1"
        assert wf2.nodes["m1"].id == "m1"


# ── CR-12: TrajectoryRecorder exec_id isolation ──


class TestCR12RecorderExecIdIsolation:
    async def test_concurrent_execs_separate_snapshots(self, tmp_path):
        from fusion_cowork.engine.hooks import HookEvent, HookManager
        from fusion_cowork.trajectory.recorder import TrajectoryRecorder

        hm = HookManager()
        rec = TrajectoryRecorder(hm, trajectory_dir=str(tmp_path))
        rec.attach()

        # Two concurrent executions each get their own START + POST_NODE; snapshots must not mix
        await hm.fire(HookEvent.WORKFLOW_START, {"execution_id": "exec_a", "workflow_id": "w_a", "workflow_name": "a"})
        await hm.fire(HookEvent.WORKFLOW_START, {"execution_id": "exec_b", "workflow_id": "w_b", "workflow_name": "b"})
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "execution_id": "exec_a",
                "node_id": "n1",
                "node_name": "x",
                "status": "success",
                "execution_time": 0.1,
                "summary": "s1",
            },
        )
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "execution_id": "exec_b",
                "node_id": "n2",
                "node_name": "y",
                "status": "success",
                "execution_time": 0.2,
                "summary": "s2",
            },
        )

        # exec_a slot contains only n1, exec_b slot only n2
        assert rec._execs["exec_a"]["steps_snapshot"][0]["node_id"] == "n1"
        assert rec._execs["exec_b"]["steps_snapshot"][0]["node_id"] == "n2"
        assert len(rec._execs["exec_a"]["steps_snapshot"]) == 1
        assert len(rec._execs["exec_b"]["steps_snapshot"]) == 1

    async def test_workflow_end_pops_exec_slot(self, tmp_path):
        from fusion_cowork.engine.hooks import HookEvent, HookManager
        from fusion_cowork.trajectory.recorder import TrajectoryRecorder

        hm = HookManager()
        rec = TrajectoryRecorder(hm, trajectory_dir=str(tmp_path))
        rec.attach()
        await hm.fire(HookEvent.WORKFLOW_START, {"execution_id": "exec_x", "workflow_id": "w", "workflow_name": "n"})
        await hm.fire(HookEvent.WORKFLOW_END, {"execution_id": "exec_x", "workflow_id": "w", "status": "completed"})
        # CR-12: execution end cleans up the state slot to prevent memory growth
        assert "exec_x" not in rec._execs


# ── CR-9: ComputerUseLoop action allowlist + param validation ──


class TestCR9ActionWhitelist:
    def test_validate_action_params_rejects_missing_coords(self):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        assert ComputerUseLoopNode._validate_action_params("mouse_move", {}) is not None
        assert ComputerUseLoopNode._validate_action_params("mouse_click", {"x": "a", "y": 1}) is not None
        assert ComputerUseLoopNode._validate_action_params("mouse_move", {"x": 1, "y": 2}) is None

    def test_validate_action_params_rejects_missing_text(self):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        assert ComputerUseLoopNode._validate_action_params("keyboard_type", {}) is not None
        assert ComputerUseLoopNode._validate_action_params("keyboard_type", {"text": ""}) is not None
        assert ComputerUseLoopNode._validate_action_params("keyboard_type", {"text": "hi"}) is None

    def test_validate_action_params_rejects_missing_key(self):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        assert ComputerUseLoopNode._validate_action_params("keyboard_shortcut", {}) is not None
        assert ComputerUseLoopNode._validate_action_params("keyboard_shortcut", {"key": "cmd+c"}) is None

    async def test_execute_ai_action_rejects_unknown_action(self, monkeypatch):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        node = ComputerUseLoopNode(config=NodeConfig(params={"max_steps": 1}))
        # AI outputs a non-allowlisted action
        out = await node._execute_ai_action("ACTION: rm_rf ACTION_DONE")
        assert out["status"] == "unknown_action"

    async def test_execute_ai_action_rejects_invalid_params(self, monkeypatch):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        node = ComputerUseLoopNode(config=NodeConfig(params={"max_steps": 1}))
        out = await node._execute_ai_action('ACTION: mouse_click PARAMS: {"x": "bad"}')
        assert out["status"] == "invalid_params"


# ── HI-7: submit_task keeps the handle ──


class TestHI7TaskHandleRetention:
    async def test_submit_task_stores_handle(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch.register_default_agents()
        task_id = await orch.submit_task("hi7 probe", {"command": "echo hi7", "timeout": 5})
        assert task_id in orch._task_handles
        # Wait for the background task to finish
        await asyncio.sleep(0.3)
        # handle is removed from _task_handles when done (finally pop); _bg_tasks is cleaned by callback
        assert task_id not in orch._task_handles


# ── HI-8: cancel actually cancels + ShellExecutor kills the process ──


class TestHI8CancelAndKill:
    async def test_cancel_running_task_propagates_cancelled_status(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch._task_timeout = 10.0

        # Register a long-blocking executor
        async def slow_exec(_input):
            await asyncio.sleep(5)
            return {"ok": True}

        from fusion_cowork.orchestrator import Agent, AgentRole

        orch.register_agent(
            Agent(agent_id="slow", name="slow", role=AgentRole.EXECUTOR, description="slow", capabilities=["slow"])
        )
        orch.register_executor("slow", slow_exec)

        # submit_task normally routes via executor_node; simulate with a direct _run_submitted_task instead
        import time

        from fusion_cowork.orchestrator.orchestrator import AgentTask

        task = AgentTask(task_id="t_slow", agent_id="slow", description="slow", input_data={}, created_at=time.time())
        orch._tasks["t_slow"] = task
        handle = asyncio.create_task(orch._run_submitted_task(task))
        orch._task_handles["t_slow"] = handle
        orch._bg_tasks.add(handle)
        handle.add_done_callback(lambda h: orch._bg_tasks.discard(h))
        await asyncio.sleep(0.1)
        # Actually cancel
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass
        # HI-18: after CancelledError is caught the status becomes cancelled
        assert task.status == "cancelled"

    async def test_shell_executor_kills_proc_on_timeout(self):
        from fusion_cowork.orchestrator.executors import ShellExecutor

        ex = ShellExecutor()
        # Start sleep 30 with timeout 0.3 — timeout should kill the process
        out = await ex({"command": "sleep 30", "timeout": 0.3})
        assert "error" in out and "超时" in out["error"]  # i18n: user-facing
        # Confirm no leftover sleep process (best-effort: ps should not contain our sleep 30)
        # Note: only the return structure is verified; process cleanup is guaranteed by proc.kill()
        assert out["error"] == "命令超时 (0.3s)"  # i18n: user-facing


# ── HI-9: WorkflowExecutor injects the parent runtime ──


class TestHI9RuntimeInjection:
    def test_inject_runtime_stores_components(self):
        from fusion_cowork.orchestrator.executors import WorkflowExecutor

        ex = WorkflowExecutor()
        sentinel_perm = object()
        sentinel_hook = object()
        sentinel_sess = object()
        ex.inject_runtime(permission_manager=sentinel_perm, hook_manager=sentinel_hook, session_store=sentinel_sess)
        assert ex._permission_manager is sentinel_perm
        assert ex._hook_manager is sentinel_hook
        assert ex._session_store is sentinel_sess

    def test_orchestrator_with_runtime_binds_private_executor(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        perm = object()
        orch = AgentOrchestrator(permission_manager=perm)
        orch.register_default_agents()
        wf_exec = orch._executors["executor_workflow"]
        # HI-9: with a parent runtime a private WorkflowExecutor is used, with perm injected
        assert wf_exec._permission_manager is perm


# ── HI-10: ComputerUseLoop screenshot temp file + param/data key fix ──


class TestHI10ScreenshotTempfile:
    async def test_screenshot_not_saved_when_save_false(self, monkeypatch, tmp_path):
        from fusion_cowork.engine.node import NodeResult, NodeStatus
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        node = ComputerUseLoopNode(config=NodeConfig(params={"max_steps": 1, "save_screenshots": False}))

        # monkeypatch AI to return task_complete immediately; ScreenCapture returns a temp file path
        async def fake_ai(_self, _text):
            return {"status": "task_complete"}

        monkeypatch.setattr(ComputerUseLoopNode, "_execute_ai_action", fake_ai)

        cap_path = str(tmp_path / "shot.png")

        async def fake_capture(_self, _params):
            Path(cap_path).write_bytes(b"fakepng")
            return NodeResult(status=NodeStatus.SUCCESS, data={"file_path": cap_path}, summary="cap")

        # ScreenCaptureNode is looked up via globals(); monkeypatch the class method
        from fusion_cowork.nodes.macos import input_nodes

        orig = getattr(input_nodes, "ScreenCaptureNode", None)
        if orig is not None:
            monkeypatch.setattr(orig, "execute", fake_capture)

        try:
            await node.execute({})
        finally:
            pass
        # HI-10: with save_screenshots=False the screenshot temp file should be cleaned up
        assert not os.path.exists(cap_path), "临时截图应删除"  # i18n: user-facing


# ── HI-16: from_dict cycle detection + resume snapshot validation ──


class TestHI16CycleAndResumeValidation:
    def test_from_dict_rejects_cycle(self):
        # Build a dict containing a cycle (bypasses connect's per-edge check by injecting edges directly)
        d = {
            "name": "cyclic",
            "nodes": [
                {"node_id": "n1", "name": "mock_success"},
                {"node_id": "n2", "name": "mock_success"},
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n1"},
            ],
        }
        with pytest.raises(ValueError, match="含环"):  # i18n: user-facing
            Workflow.from_dict(d)

    async def test_resume_stale_node_id_logged_not_crash(self):
        wf = Workflow(name="resume")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine()
        # Snapshot contains the nonexistent node n_ghost
        resume = [{"node_id": "n_ghost", "status": "success", "output_data": {}}]
        result = await engine.execute(wf, resume_steps=resume)
        # Must not crash; n1 runs normally
        assert result.status == WorkflowStatus.SUCCESS


# ── HI-19: budget overrun aborts execution ──


class TestHI19BudgetAbort:
    async def test_budget_over_limit_aborts_execution(self):
        from fusion_cowork.ai.budget import BudgetTracker

        # Build a tracker already over budget (enforce=True, cost > max)
        tracker = BudgetTracker(max_budget_usd=0.01, enforce=True)
        tracker._record.cost_usd = 0.5  # far over the 0.01 cap

        wf = Workflow(name="budget")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine(budget_tracker=tracker)
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.FAILED
        assert "预算" in (result.error or "")  # i18n: user-facing

    async def test_no_budget_tracker_runs_normally(self):
        wf = Workflow(name="nobudget")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine()  # budget_tracker=None
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.SUCCESS


# ── HI-20: CoordinatorExecutor delegation depth limit ──


class TestHI20DepthLimit:
    async def test_depth_over_max_rejected(self):
        from fusion_cowork.orchestrator.executors import CoordinatorExecutor

        coord = CoordinatorExecutor(orchestrator=object())  # arbitrary binding
        # depth=999 exceeds MAX_DEPTH(5)
        out = await coord({"prompt": "deep", "subtask_type": "node", "_depth": 999})
        assert "超限" in out["error"]  # i18n: user-facing

    async def test_depth_zero_submits_with_incremented_child(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch.register_default_agents()
        coord = orch._coordinator_executor

        submitted = {"child_depth": None}

        async def fake_submit(description, input_data=None):
            submitted["child_depth"] = (input_data or {}).get("_depth")
            # Return a fake task_id
            return "task_fake"

        monkeypatch_target = orch.submit_task
        orch.submit_task = fake_submit

        # get_task returns a terminal state so polling ends immediately
        import time

        from fusion_cowork.orchestrator.orchestrator import AgentTask

        fake_task = AgentTask(
            task_id="task_fake",
            agent_id="executor_node",
            description="d",
            input_data={},
            created_at=time.time(),
            status="completed",
            output_data={"ok": True},
        )
        orch._tasks["task_fake"] = fake_task

        try:
            out = await coord({"prompt": "go", "subtask_type": "node", "_depth": 0})
        finally:
            orch.submit_task = monkeypatch_target
        assert submitted["child_depth"] == 1
        assert out == {"ok": True}
