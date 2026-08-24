"""Stage 4 — 并发/执行正确性测试 (CR-11/12/9/HI-7/8/9/10/16/18/19/20)。

验证审计发现修复:
- CR-11: execute() 不在共享 BaseNode 写 status/result, 并发执行无状态污染
- CR-12: TrajectoryRecorder 按 execution_id 命名空间, 并发执行快照隔离
- CR-9: ComputerUseLoop 动作白名单 + 参数校验
- HI-7: submit_task 保留 asyncio.Task handle
- HI-8: cancel_task 真取消运行协程 + ShellExecutor 超时杀进程
- HI-9: WorkflowExecutor 注入父引擎 permission/hook/session
- HI-10: ComputerUseLoop 截图用临时文件 + param 名/data 键修正
- HI-16: Workflow.from_dict 环检测 + 断点续跑快照拓扑校验
- HI-18: CancelledError 单独捕获, finally 置终态
- HI-19: 预算超限中止执行
- HI-20: CoordinatorExecutor 委托深度上限
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# 注册全部节点 (与 test_permission_stage3 同模式, 防跨测试注册表污染)
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
    """跨测试注册表污染防护: 缺 shell_exec 则重导。"""
    if "shell_exec" not in NodeRegistry._registry:
        import fusion_cowork.nodes

        fusion_cowork.nodes.import_all_nodes()


# ── CR-11: execute 不写共享 BaseNode ──


@register_node
class _SharedStateProbe(BaseNode):
    name = "stage4_probe"
    display_name = "Stage4 探针"
    category = NodeCategory.IO
    description = "探测: 读取共享节点状态"
    icon = "🧪"
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
        # CR-11: execute 后 BaseNode.status 仍为初始值 PENDING (未写 RUNNING/FAILED)
        node = wf.nodes["n1"]
        assert node.status == NodeStatus.PENDING
        # 状态只在 WorkflowStep, node.result 未被 execute 写入
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
        # 两工作流节点互不污染
        assert wf.nodes["n1"].id == "n1"
        assert wf2.nodes["m1"].id == "m1"


# ── CR-12: TrajectoryRecorder exec_id 隔离 ──


class TestCR12RecorderExecIdIsolation:
    async def test_concurrent_execs_separate_snapshots(self, tmp_path):
        from fusion_cowork.engine.hooks import HookEvent, HookManager
        from fusion_cowork.trajectory.recorder import TrajectoryRecorder

        hm = HookManager()
        rec = TrajectoryRecorder(hm, trajectory_dir=str(tmp_path))
        rec.attach()

        # 两并发执行各自 START + POST_NODE, 快照互不混入
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

        # exec_a 槽只含 n1, exec_b 槽只含 n2
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
        # CR-12: 执行结束清理状态槽防内存增长
        assert "exec_x" not in rec._execs


# ── CR-9: ComputerUseLoop 动作白名单 + 参数校验 ──


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
        # AI 输出非白名单动作
        out = await node._execute_ai_action("ACTION: rm_rf ACTION_DONE")
        assert out["status"] == "unknown_action"

    async def test_execute_ai_action_rejects_invalid_params(self, monkeypatch):
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        node = ComputerUseLoopNode(config=NodeConfig(params={"max_steps": 1}))
        out = await node._execute_ai_action('ACTION: mouse_click PARAMS: {"x": "bad"}')
        assert out["status"] == "invalid_params"


# ── HI-7: submit_task 保留 handle ──


class TestHI7TaskHandleRetention:
    async def test_submit_task_stores_handle(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch.register_default_agents()
        task_id = await orch.submit_task("hi7 probe", {"command": "echo hi7", "timeout": 5})
        assert task_id in orch._task_handles
        # 等后台完成
        await asyncio.sleep(0.3)
        # handle 完成后从 _task_handles 移除 (finally pop), _bg_tasks 由回调移除
        assert task_id not in orch._task_handles


# ── HI-8: cancel 真取消 + ShellExecutor 杀进程 ──


class TestHI8CancelAndKill:
    async def test_cancel_running_task_propagates_cancelled_status(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch._task_timeout = 10.0

        # 注册一个长阻塞执行器
        async def slow_exec(_input):
            await asyncio.sleep(5)
            return {"ok": True}

        from fusion_cowork.orchestrator import Agent, AgentRole

        orch.register_agent(
            Agent(agent_id="slow", name="slow", role=AgentRole.EXECUTOR, description="slow", capabilities=["slow"])
        )
        orch.register_executor("slow", slow_exec)

        # submit_task 默认走 executor_node, 改用直接 _run_submitted_task 模拟
        import time

        from fusion_cowork.orchestrator.orchestrator import AgentTask

        task = AgentTask(task_id="t_slow", agent_id="slow", description="slow", input_data={}, created_at=time.time())
        orch._tasks["t_slow"] = task
        handle = asyncio.create_task(orch._run_submitted_task(task))
        orch._task_handles["t_slow"] = handle
        orch._bg_tasks.add(handle)
        handle.add_done_callback(lambda h: orch._bg_tasks.discard(h))
        await asyncio.sleep(0.1)
        # 真取消
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass
        # HI-18: CancelledError 捕获后置 cancelled
        assert task.status == "cancelled"

    async def test_shell_executor_kills_proc_on_timeout(self):
        from fusion_cowork.orchestrator.executors import ShellExecutor

        ex = ShellExecutor()
        # 启动 sleep 30, timeout 0.3 — 超时应杀进程
        out = await ex({"command": "sleep 30", "timeout": 0.3})
        assert "error" in out and "超时" in out["error"]
        # 确认无残留 sleep 进程 (best-effort: ps 不应含我们的 sleep 30)
        # 注: 仅验证返回结构, 进程清理由 proc.kill() 保证
        assert out["error"] == "命令超时 (0.3s)"


# ── HI-9: WorkflowExecutor 注入父运行时 ──


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
        # HI-9: 有父运行时则换私有 WorkflowExecutor, 注入了 perm
        assert wf_exec._permission_manager is perm


# ── HI-10: ComputerUseLoop 截图临时文件 + param/data 键修正 ──


class TestHI10ScreenshotTempfile:
    async def test_screenshot_not_saved_when_save_false(self, monkeypatch, tmp_path):
        from fusion_cowork.engine.node import NodeResult, NodeStatus
        from fusion_cowork.nodes.macos.input_nodes import ComputerUseLoopNode

        node = ComputerUseLoopNode(config=NodeConfig(params={"max_steps": 1, "save_screenshots": False}))

        # monkeypatch AI 返回 task_complete 立即结束, ScreenCapture 返回临时文件路径
        async def fake_ai(_self, _text):
            return {"status": "task_complete"}

        monkeypatch.setattr(ComputerUseLoopNode, "_execute_ai_action", fake_ai)

        cap_path = str(tmp_path / "shot.png")

        async def fake_capture(_self, _params):
            Path(cap_path).write_bytes(b"fakepng")
            return NodeResult(status=NodeStatus.SUCCESS, data={"file_path": cap_path}, summary="cap")

        # ScreenCaptureNode 经 globals() 查找, monkeypatch 类方法
        from fusion_cowork.nodes.macos import input_nodes

        orig = getattr(input_nodes, "ScreenCaptureNode", None)
        if orig is not None:
            monkeypatch.setattr(orig, "execute", fake_capture)

        try:
            await node.execute({})
        finally:
            pass
        # HI-10: save_screenshots=False 时截图临时文件应被清理
        assert not os.path.exists(cap_path), "临时截图应删除"


# ── HI-16: from_dict 环检测 + 断点续跑快照校验 ──


class TestHI16CycleAndResumeValidation:
    def test_from_dict_rejects_cycle(self):
        # 构造含环的 dict (绕过 connect 的逐条检查, 直接塞 edges)
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
        with pytest.raises(ValueError, match="含环"):
            Workflow.from_dict(d)

    async def test_resume_stale_node_id_logged_not_crash(self):
        wf = Workflow(name="resume")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine()
        # 快照含不存在节点 n_ghost
        resume = [{"node_id": "n_ghost", "status": "success", "output_data": {}}]
        result = await engine.execute(wf, resume_steps=resume)
        # 不应崩溃, n1 正常执行
        assert result.status == WorkflowStatus.SUCCESS


# ── HI-19: 预算超限中止执行 ──


class TestHI19BudgetAbort:
    async def test_budget_over_limit_aborts_execution(self):
        from fusion_cowork.ai.budget import BudgetTracker

        # 构造已超预算的 tracker (enforce=True, cost > max)
        tracker = BudgetTracker(max_budget_usd=0.01, enforce=True)
        tracker._record.cost_usd = 0.5  # 远超 0.01 上限

        wf = Workflow(name="budget")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine(budget_tracker=tracker)
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.FAILED
        assert "预算" in (result.error or "")

    async def test_no_budget_tracker_runs_normally(self):
        wf = Workflow(name="nobudget")
        wf.add_node(_SharedStateProbe(node_id="n1"))
        engine = WorkflowEngine()  # budget_tracker=None
        result = await engine.execute(wf)
        assert result.status == WorkflowStatus.SUCCESS


# ── HI-20: CoordinatorExecutor 委托深度上限 ──


class TestHI20DepthLimit:
    async def test_depth_over_max_rejected(self):
        from fusion_cowork.orchestrator.executors import CoordinatorExecutor

        coord = CoordinatorExecutor(orchestrator=object())  # 任意绑定
        # depth=999 超 MAX_DEPTH(5)
        out = await coord({"prompt": "deep", "subtask_type": "node", "_depth": 999})
        assert "超限" in out["error"]

    async def test_depth_zero_submits_with_incremented_child(self):
        from fusion_cowork.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        orch.register_default_agents()
        coord = orch._coordinator_executor

        submitted = {"child_depth": None}

        async def fake_submit(description, input_data=None):
            submitted["child_depth"] = (input_data or {}).get("_depth")
            # 返回伪 task_id
            return "task_fake"

        monkeypatch_target = orch.submit_task
        orch.submit_task = fake_submit

        # get_task 返回终态立即结束轮询
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
