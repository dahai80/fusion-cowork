"""issue #218: the web_api scheduled-task executor must satisfy TaskScheduler's zero-arg call convention.

The old _exec(task_id) signature mismatches scheduler._run_task's await executor(); cron/interval
tasks raised TypeError on every fire, and after 5 consecutive failures the task was marked FAILED
with no run produced. Regression anchor: firing a task once must leave fail_count at 0 and actually
create and execute a run."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.scheduler import TaskScheduler
from fusion_cowork.engine.workflow import WorkflowEngine

PROBE_NODE = "sched_probe_218"


@register_node
class _SchedProbeNode(BaseNode):
    name = PROBE_NODE
    display_name = "Sched Probe 218"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "测试用: 低风险调度探针 (#218)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-218")
    from fusion_cowork.server import web_api

    eng = WorkflowEngine()
    sched = TaskScheduler()
    app = web_api.create_web_app(
        engine=eng, scheduler=sched, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store"
    )
    return app, sched


class TestSchedulerExecutorSignature:
    async def test_task_fire_creates_run(self, web):
        from fastapi.testclient import TestClient

        app, sched = web
        auth = {"Authorization": "Bearer test-key-218"}
        _SchedProbeNode.calls = 0
        with TestClient(app) as client:
            wf_id = client.post(
                "/api/workflows",
                json={
                    "name": "sched-wf-218",
                    "workflow": {"nodes": [{"id": "n1", "name": PROBE_NODE, "config": {}}], "edges": []},
                },
                headers=auth,
            ).json()["id"]
            resp = client.post(
                "/api/scheduler/tasks",
                json={"name": "t218", "workflow_id": wf_id, "interval_minutes": 5},
                headers=auth,
            )
            assert resp.status_code == 201, resp.text
            task_id = resp.json()["id"]

            await sched._run_task(task_id)

            task = sched.get_task(task_id)
            assert task.fail_count == 0, f"触发失败: fail_count={task.fail_count}"  # i18n: user-facing
            assert task.run_count == 1
            runs = []
            for _ in range(100):
                runs = client.get("/api/runs", headers=auth).json()["runs"]
                if runs and runs[0].get("status") != "running":
                    break
                await asyncio.sleep(0.02)
            assert runs, "执行器未产生任何 run"  # i18n: user-facing
            assert runs[0]["status"] == "success", runs[0]
            assert runs[0]["source"] == ""
            assert _SchedProbeNode.calls == 1

    async def test_executor_is_zero_arg_callable(self, web):
        from fastapi.testclient import TestClient

        app, sched = web
        auth = {"Authorization": "Bearer test-key-218"}
        with TestClient(app) as client:
            wf_id = client.post(
                "/api/workflows",
                json={
                    "name": "sched-wf-218b",
                    "workflow": {"nodes": [{"id": "n1", "name": PROBE_NODE, "config": {}}], "edges": []},
                },
                headers=auth,
            ).json()["id"]
            task_id = client.post(
                "/api/scheduler/tasks",
                json={"name": "t218b", "workflow_id": wf_id, "cron": "0 3 * * *"},
                headers=auth,
            ).json()["id"]
            executor = sched._executors[task_id]
            # TaskScheduler._run_task call convention: await executor() — zero args
            params = inspect.signature(executor).parameters
            required = [p for p in params.values() if p.default is inspect.Parameter.empty]
            assert not required, f"executor 声明了必填参数: {list(params)}"  # i18n: user-facing
