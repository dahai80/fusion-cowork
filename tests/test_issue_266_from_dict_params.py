"""issue #266: from_dict 丢弃模板/API 顶层 params — HITL 批准恢复后节点空参数重跑。"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine.node import NodeRegistry
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


class TestFromDictParams:
    def test_toplevel_params_restored(self):
        n = NodeRegistry.get("shell_exec").from_dict(
            {"id": "n1", "name": "shell_exec", "params": {"command": "echo hi"}}
        )
        assert n.config.params == {"command": "echo hi"}

    def test_config_params_still_win(self):
        n = NodeRegistry.get("shell_exec").from_dict(
            {"id": "n1", "name": "shell_exec", "config": {"params": {"command": "a"}}, "params": {"command": "b"}}
        )
        assert n.config.params == {"command": "a"}

    def test_missing_params_empty(self):
        n = NodeRegistry.get("shell_exec").from_dict({"id": "n1", "name": "shell_exec"})
        assert n.config.params == {}


class TestHitlResumeWithParams:
    @pytest.mark.asyncio
    async def test_approved_node_reruns_with_params(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = Workflow.from_dict(
            {
                "name": "i266",
                "nodes": [{"id": "n1", "name": "shell_exec", "params": {"command": "echo hello-266"}}],
            }
        )
        task = asyncio.create_task(eng.execute(wf, execution_id="run_266a"))
        for _ in range(200):
            if eng.hitl_pending("run_266a"):
                break
            await asyncio.sleep(0.01)
        assert eng.hitl_pending("run_266a")
        pm = eng._permission_manager
        details = pm.find_pending_by_execution("run_266a")
        assert details is not None
        # 待审批卡应带真实参数 (修复前 params={} — 节点参数在反序列化时已丢失)
        assert details["params"].get("command") == "echo hello-266"
        eng.resolve_hitl("run_266a", True, once=True)
        execution = await task
        assert execution.status == WorkflowStatus.SUCCESS
        assert "hello-266" in execution.steps[-1].output_data.get("stdout", "")
