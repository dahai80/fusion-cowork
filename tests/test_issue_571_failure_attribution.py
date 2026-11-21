# issue #571: MAST failure attribution in the DAG node failure path.
#
# FUSION_COWORK_FAILURE_ATTRIBUTION=1 → failed nodes carry a MAST verdict
# (mode/cluster/confidence/evidence) in their result data + the run summary;
# spec-class failures (specification_system_design cluster) skip the remaining
# bounded retries instead of burning budget. Flag off = current behavior.

import pytest

from fusion_cowork.engine.kernel_gates import (
    attribute_node_failure,
    failure_attribution_enabled,
    is_spec_failure,
)
from fusion_cowork.engine.node import NodeRegistry
from fusion_cowork.engine.workflow import WorkflowEngine, WorkflowStatus

SPEC_ERR = "ambiguous task: no objective provided"
GENERIC_ERR = "boom"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_FAILURE_ATTRIBUTION", raising=False)
    yield


def _wf(node_name, max_retries=2, retry_business=True):
    from fusion_cowork.engine.node import NodeConfig
    from fusion_cowork.engine.workflow import Workflow

    wf = Workflow(name=f"wf-571-{node_name}")
    node = NodeRegistry.create(node_name)
    node.config = NodeConfig(max_retries=max_retries, retry_on_business_failure=retry_business)
    wf.add_node(node)
    return wf


def _register_failing_cfg(name, error):
    """Register a node class whose every execute() fails with `error`.

    Returns the class; its `calls` attribute counts execute() invocations.
    """
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

    async def _execute(self, inputs):
        _execute.calls.append(1)
        return NodeResult(status=NodeStatus.FAILED, error=error)

    _execute.calls = []
    cls = type(
        f"_Fail571_{name}",
        (BaseNode,),
        {
            "name": name,
            "display_name": name,
            "category": NodeCategory.TOOL,
            "description": "test-only failing node",
            "icon": "x",
            "default_label": name,
            "execute": _execute,
        },
    )
    register_node(cls)
    return cls


class TestHelper:
    def test_flag_default_off(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_FAILURE_ATTRIBUTION", raising=False)
        assert failure_attribution_enabled() is False

    def test_flag_on(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_FAILURE_ATTRIBUTION", "1")
        assert failure_attribution_enabled() is True

    def test_attribute_error_record_yields_verdict(self):
        verdict = attribute_node_failure("run-571-h", [], "n9", "boom")
        assert verdict is not None
        assert verdict["mode"]
        assert verdict["cluster"]
        assert "confidence" in verdict
        assert verdict["agent"] == "n9"

    def test_spec_cluster_detection(self):
        verdict = attribute_node_failure("run-571-s", [], "n1", SPEC_ERR)
        assert verdict is not None
        assert is_spec_failure(verdict) is True

    def test_non_spec_failure_not_spec(self):
        verdict = attribute_node_failure("run-571-g", [], "n1", GENERIC_ERR)
        assert verdict is not None
        assert is_spec_failure(verdict) is False


class TestEngineAttribution:
    async def test_flag_off_no_attribution(self, tmp_path):
        cls = _register_failing_cfg("fail_571_off", GENERIC_ERR)
        eng = WorkflowEngine(permission_manager=None)
        execution = await eng.execute(_wf("fail_571_off"), execution_id="run-571-off")
        assert execution.status == WorkflowStatus.FAILED
        assert execution.attribution is None
        assert len(cls.execute.calls) == 3  # retries proceed as before

    async def test_flag_on_spec_failure_skips_retries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_FAILURE_ATTRIBUTION", "1")
        cls = _register_failing_cfg("fail_571_spec", SPEC_ERR)
        eng = WorkflowEngine(permission_manager=None)
        execution = await eng.execute(_wf("fail_571_spec"), execution_id="run-571-spec")
        assert execution.status == WorkflowStatus.FAILED
        assert len(cls.execute.calls) == 1, "spec-class failure must not burn retry budget"
        assert execution.attribution is not None
        assert is_spec_failure(execution.attribution)
        assert execution.attribution["mode"]

    async def test_flag_on_generic_failure_still_retries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_FAILURE_ATTRIBUTION", "1")
        cls = _register_failing_cfg("fail_571_gen", GENERIC_ERR)
        eng = WorkflowEngine(permission_manager=None)
        execution = await eng.execute(_wf("fail_571_gen"), execution_id="run-571-gen")
        assert execution.status == WorkflowStatus.FAILED
        assert len(cls.execute.calls) == 3, "non-spec failures keep bounded retries"
        assert execution.attribution is not None
        assert is_spec_failure(execution.attribution) is False

    async def test_attribution_in_node_result_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_FAILURE_ATTRIBUTION", "1")
        _register_failing_cfg("fail_571_data", SPEC_ERR)
        eng = WorkflowEngine(permission_manager=None)
        execution = await eng.execute(_wf("fail_571_data"), execution_id="run-571-data")
        failed = [s for s in execution.steps if s.status.value == "failed"]
        assert failed
        assert failed[0].output_data.get("attribution", {}).get("cluster")
