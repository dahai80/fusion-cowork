# issue #494 Part A: node substrate delegates to fusion-harness dag_node.
#
# A1 — BaseNode subclasses the kernel BaseNode; NodeRegistry routes
#      registration through a kernel NodeRegistry instance (dup-reject
#      enforcement = kernel code). Cowork hardening (aliases, protected
#      names, builtin freeze, lenient coercion) stays as documented parity
#      extensions.
# A2 — recovery replay: mid-run crash on a 3-node chain, same execution_id
#      re-dispatch replays committed nodes via KernelCheckpointBinder
#      receipts (committed effects never re-execute).

import pytest

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
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

try:
    from fusion_harness.dag_node import BaseNode as KernelBaseNode
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"fusion-harness missing: {exc}", allow_module_level=True)

COUNT_494 = "substrate_count_494"
TYPE_494 = "substrate_typed_494"
FLIP_494 = "substrate_flip_494"


@register_node
class _CountProbe(BaseNode):
    name = COUNT_494
    display_name = "Substrate Count 494"
    category = NodeCategory.LOGIC
    description = "test-only execution counter (#494)"
    icon = "🧪"
    default_label = "Count494"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        _CountProbe.calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"calls": _CountProbe.calls}, summary="counted")


@register_node
class _TypedProbe(BaseNode):
    name = TYPE_494
    display_name = "Substrate Typed 494"
    category = NodeCategory.LOGIC
    description = "test-only typed-params probe (#494)"
    icon = "🧪"
    default_label = "Typed494"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data=dict(inputs), summary="typed")

    def get_params_schema(self):
        return {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}


@register_node
class _FlipProbe(BaseNode):
    name = FLIP_494
    display_name = "Substrate Flip 494"
    category = NodeCategory.LOGIC
    description = "test-only fail-once probe (#494)"
    icon = "🧪"
    default_label = "Flip494"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        _FlipProbe.calls += 1
        if _FlipProbe.calls == 1:
            return NodeResult(status=NodeStatus.FAILED, error="flip 494 intentional first-run failure")
        return NodeResult(status=NodeStatus.SUCCESS, data={"attempt": _FlipProbe.calls}, summary="flipped")


class TestA1KernelSubstrate:
    def test_base_node_subclasses_kernel(self):
        assert issubclass(BaseNode, KernelBaseNode)
        node = _CountProbe()
        # kernel contract: id auto-generated, node_type attribute present
        assert node.id.startswith("node_")
        assert isinstance(node.node_type, str)

    def test_registry_backed_by_kernel_instance(self):
        # fresh probe per test — suite ordering may clear/rebuild global state
        class _Fresh(BaseNode):
            name = "substrate_fresh_494"
            category = NodeCategory.LOGIC

            async def execute(self, inputs: dict) -> NodeResult:
                return NodeResult(status=NodeStatus.SUCCESS, data={}, summary="ok")

        returned = NodeRegistry.register(_Fresh)
        assert returned is _Fresh
        assert "substrate_fresh_494" in NodeRegistry._kernel_registry.list()
        assert NodeRegistry.get("substrate_fresh_494") is _Fresh
        # kernel stamps node_type on registration
        assert _Fresh.node_type == "substrate_fresh_494"
        NodeRegistry.unregister("substrate_fresh_494")

    def test_dup_register_rejected_keeping_original(self):
        class _Impostor(_CountProbe):
            name = COUNT_494

        returned = NodeRegistry.register(_Impostor)
        assert returned is _Impostor  # silent no-raise return (CR-20 semantics)
        assert NodeRegistry.get(COUNT_494) is _CountProbe  # original kept

    def test_alias_and_create_coercion_unchanged(self):
        NodeRegistry.register_alias("计数器494", COUNT_494)
        assert NodeRegistry.resolve_alias("计数器494") == COUNT_494
        node = NodeRegistry.create(TYPE_494, node_id="t1", config=NodeConfig(params={"limit": "10"}))
        assert node.config.params["limit"] == 10  # lenient local coercion intact

    def test_unregister_rebuilds_kernel_registry(self):
        class _Ephemeral(BaseNode):
            name = "substrate_ephemeral_494"
            category = NodeCategory.LOGIC

            async def execute(self, inputs: dict) -> NodeResult:
                return NodeResult(status=NodeStatus.SUCCESS, data={}, summary="ok")

        NodeRegistry.register(_Ephemeral)
        assert "substrate_ephemeral_494" in NodeRegistry._kernel_registry.list()
        NodeRegistry.unregister("substrate_ephemeral_494")
        assert "substrate_ephemeral_494" not in NodeRegistry._kernel_registry.list()
        assert NodeRegistry.get("substrate_ephemeral_494") is None

    def test_force_reregister_replaces_in_kernel(self):
        original = NodeRegistry.get(COUNT_494)
        try:
            NodeRegistry.register(_CountProbe, force=True)
            assert NodeRegistry.get(COUNT_494) is _CountProbe
            assert NodeRegistry._kernel_registry.get(COUNT_494) is _CountProbe
        finally:
            NodeRegistry.register(original, force=True)


class TestA2RecoveryReplay:
    async def test_mid_run_crash_replay_same_execution_id(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        binder = KernelCheckpointBinder()
        try:
            eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, kernel_checkpoints=binder)
            wf = Workflow(name="wf-494-replay")
            wf.add_node(NodeRegistry.create(COUNT_494, node_id="a1"))
            wf.add_node(NodeRegistry.create(FLIP_494, node_id="a2"))
            wf.connect("a1", "a2")
            wf.add_node(NodeRegistry.create(COUNT_494, node_id="a3"))
            wf.connect("a2", "a3")

            _CountProbe.calls = 0
            _FlipProbe.calls = 0
            ex1 = await eng.execute(wf, execution_id="run_494_replay")
            assert ex1.status.value == "failed"
            assert _CountProbe.calls == 1  # a1 committed exactly once
            assert _FlipProbe.calls == 1

            # same execution_id re-dispatch: a1 replays from receipt (no
            # re-execution), a2 retries, a3 runs — chain completes
            ex2 = await eng.execute(wf, execution_id="run_494_replay")
            assert ex2.status.value == "success"
            assert _CountProbe.calls == 2  # a3 executed; a1 NOT re-executed
            assert _FlipProbe.calls == 2
            a1_step = next(s for s in ex2.steps if s.node_id == "a1")
            assert a1_step.status.value == "skipped"
            assert "replayed from kernel checkpoint" in (a1_step.summary or "")
            assert a1_step.output_data == {"calls": 1}
            assert binder.has_receipt("run_494_replay", "a3")
        finally:
            binder.close()

    async def test_no_binder_reruns_everything(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        wf = Workflow(name="wf-494-nobinder")
        wf.add_node(NodeRegistry.create(COUNT_494, node_id="b1"))
        _CountProbe.calls = 0
        await eng.execute(wf, execution_id="run_494_nobinder")
        ex2 = await eng.execute(wf, execution_id="run_494_nobinder")
        assert ex2.status.value == "success"
        assert _CountProbe.calls == 2  # no binder = no receipt replay
