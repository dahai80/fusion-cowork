# Run 级记忆集成 e2e (issue #157 / fusion-memory#20 验收)。
# 需真 fm-server: FUSION_MEMORY_E2E=1 + FUSION_MEMORY_API_KEY + (可选 FUSION_MEMORY_BASE_URL)。
# 未设 FUSION_MEMORY_E2E → skip (CI 默认离线)。运行示例:
#   FUSION_MEMORY_E2E=1 FUSION_MEMORY_API_KEY=xxx pytest tests/test_memory_e2e.py -v

import os
import uuid

import pytest

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry
from tests.test_engine import MockSuccessNode

pytestmark = pytest.mark.skipif(
    os.environ.get("FUSION_MEMORY_E2E", "") != "1",
    reason="e2e 需真 fm-server: 设 FUSION_MEMORY_E2E=1 + FUSION_MEMORY_API_KEY",
)


def _make_registry_and_def():
    engine = WorkflowEngine()
    registry = RunRegistry(engine)
    wf = Workflow(name="财报任务", description="整理季度财报")
    wf.add_node(MockSuccessNode(node_id="n1"))
    return registry, wf.to_dict()


@pytest.mark.asyncio
async def test_two_consecutive_runs_second_learns_from_first(monkeypatch):
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    # key 来自真环境 (已设); 清掉跑假 transport 的可能 — 这里必须走真 HTTP
    assert os.environ.get("FUSION_MEMORY_API_KEY", "").strip(), "缺 FUSION_MEMORY_API_KEY"

    session = f"cowork_e2e_{uuid.uuid4().hex[:8]}"
    marker = f"E2E-157-{uuid.uuid4().hex[:6]}"

    # task1: 直接经 bridge 写回产出 (RunRegistry 写回取 steps 摘要, marker 经 summary 进入)
    from fusion_cowork.memory_bridge import MemoryBridge

    bridge = MemoryBridge.from_env()
    assert bridge is not None
    interaction = MemoryBridge.build_interaction(
        session_id=session,
        workflow_id="wf-e2e-157",
        workflow_name="财报任务",
        description="整理季度财报",
        steps=[],
        result_summary=f"{marker} 用户偏好: 财务周报必须用表格输出",
        execution_id=session,
    )
    ids = await bridge.write_back(interaction)
    assert ids, f"task1 写回失败: {ids}"

    # task2: 新 run 启动召回, 应含 task1 学到的偏好
    registry, wf_def = _make_registry_and_def()
    ret = registry.start(wf_def, workflow_id="wf-e2e-157", workflow_name="财报任务", memory=True)
    await registry._tasks[ret["run_id"]]
    record = registry.get(ret["run_id"])
    assert record["status"] == "success"
    assert marker in (record.get("memory_context") or ""), (
        f"task2 未召回 task1 内容: context={record.get('memory_context', '')[:200]}"
    )
    assert record.get("memory_writeback") == "ok"


@pytest.mark.asyncio
async def test_memory_off_leaves_no_trace(monkeypatch):
    assert os.environ.get("FUSION_MEMORY_API_KEY", "").strip(), "缺 FUSION_MEMORY_API_KEY"
    registry, wf_def = _make_registry_and_def()
    ret = registry.start(wf_def, workflow_id="wf-e2e-157", workflow_name="财报任务", memory=False)
    await registry._tasks[ret["run_id"]]
    record = registry.get(ret["run_id"])
    assert record["status"] == "success"
    assert "memory_context" not in record
    assert "memory_writeback" not in record
