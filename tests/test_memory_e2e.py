# Run-level memory integration e2e (issue #157 / fusion-memory#20 acceptance).
# Requires a real fm-server: FUSION_MEMORY_E2E=1 + FUSION_MEMORY_API_KEY + (optional FUSION_MEMORY_BASE_URL).
# Without FUSION_MEMORY_E2E -> skip (CI runs offline by default). Run example:
#   FUSION_MEMORY_E2E=1 FUSION_MEMORY_API_KEY=xxx pytest tests/test_memory_e2e.py -v

import os
import uuid

import pytest

from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry
from tests.test_engine import MockSuccessNode

pytestmark = pytest.mark.skipif(
    os.environ.get("FUSION_MEMORY_E2E", "") != "1",
    reason="e2e 需真 fm-server: 设 FUSION_MEMORY_E2E=1 + FUSION_MEMORY_API_KEY",  # i18n: user-facing
)


def _make_registry_and_def():
    engine = WorkflowEngine()
    registry = RunRegistry(engine)
    wf = Workflow(name="财报任务", description="整理季度财报")  # i18n: user-facing
    wf.add_node(MockSuccessNode(node_id="n1"))
    return registry, wf.to_dict()


@pytest.mark.asyncio
async def test_two_consecutive_runs_second_learns_from_first(monkeypatch):
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)
    # key comes from the real environment (already set); rule out a fake transport — this must go over real HTTP
    assert os.environ.get("FUSION_MEMORY_API_KEY", "").strip(), "缺 FUSION_MEMORY_API_KEY"  # i18n: user-facing

    session = f"cowork_e2e_{uuid.uuid4().hex[:8]}"
    marker = f"E2E-157-{uuid.uuid4().hex[:6]}"

    # task1: write back output directly via the bridge (RunRegistry write-back takes step summaries; the marker enters via summary)
    from fusion_cowork.memory_bridge import MemoryBridge

    bridge = MemoryBridge.from_env()
    assert bridge is not None
    interaction = MemoryBridge.build_interaction(
        session_id=session,
        workflow_id="wf-e2e-157",
        workflow_name="财报任务",  # i18n: user-facing
        description="整理季度财报",  # i18n: user-facing
        steps=[],
        result_summary=f"{marker} 用户偏好: 财务周报必须用表格输出",  # i18n: user-facing
        execution_id=session,
    )
    ids = await bridge.write_back(interaction)
    assert ids, f"task1 写回失败: {ids}"  # i18n: user-facing

    # task2: a new run recalls at startup and should contain the preference learned by task1
    registry, wf_def = _make_registry_and_def()
    ret = registry.start(wf_def, workflow_id="wf-e2e-157", workflow_name="财报任务", memory=True)  # i18n: user-facing
    await registry._tasks[ret["run_id"]]
    record = registry.get(ret["run_id"])
    assert record["status"] == "success"
    assert marker in (record.get("memory_context") or ""), (
        f"task2 未召回 task1 内容: context={record.get('memory_context', '')[:200]}"  # i18n: user-facing
    )
    assert record.get("memory_writeback") == "ok"


@pytest.mark.asyncio
async def test_memory_off_leaves_no_trace(monkeypatch):
    assert os.environ.get("FUSION_MEMORY_API_KEY", "").strip(), "缺 FUSION_MEMORY_API_KEY"  # i18n: user-facing
    registry, wf_def = _make_registry_and_def()
    ret = registry.start(wf_def, workflow_id="wf-e2e-157", workflow_name="财报任务", memory=False)  # i18n: user-facing
    await registry._tasks[ret["run_id"]]
    record = registry.get(ret["run_id"])
    assert record["status"] == "success"
    assert "memory_context" not in record
    assert "memory_writeback" not in record
