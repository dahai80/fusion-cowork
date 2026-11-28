import json
from types import SimpleNamespace

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow
from fusion_cowork.trajectory.compile import compile_workflow
from fusion_cowork.trajectory.plan_cache import (
    PlanLibrary,
    cached_compile,
    get_plan_library,
    plan_cache_enabled,
    plan_covers,
    plan_signature,
)

# CLAIM: verified by tests/test_plan_cache.py

STEPS = [
    {"node_name": "risk_probe", "status": "success", "output_data": {"risk": 8}, "error": ""},
    {"node_name": "sink", "status": "success", "output_data": {"done": True}, "error": ""},
]


class _Producer(BaseNode):
    name = "risk_probe"
    display_name = "risk probe"
    category = NodeCategory.IO
    description = "test: emits a risk value"
    icon = "T"
    default_label = "risk probe"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"risk": 8})


class _Sink(BaseNode):
    name = "sink"
    display_name = "sink"
    category = NodeCategory.IO
    description = "test: records it ran"
    icon = "T"
    default_label = "sink"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True})


def _make_factory():
    def create_node(name: str) -> BaseNode:
        if name == "risk_probe":
            return _Producer()
        if name == "sink":
            return _Sink()
        raise ValueError(f"unknown node: {name}")

    return create_node


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.delenv("FUSION_COWORK_PLAN_CACHE", raising=False)
    lib = get_plan_library()
    lib._plans.clear()
    lib.hits = 0
    lib.misses = 0
    yield


class TestSignature:
    def test_order_insensitive_multiset(self):
        a = plan_signature(STEPS)
        b = plan_signature(list(reversed(STEPS)))
        assert a == ("risk_probe", "sink")
        assert a == b

    def test_empty_and_unnamed_steps(self):
        assert plan_signature([]) == ()
        assert plan_signature([{"node_name": ""}, {"node_name": "x"}]) == ("x",)

    def test_object_step_shape(self):
        sig = plan_signature([SimpleNamespace(node_name="a"), {"node_name": "b"}])
        assert sig == ("a", "b")


class TestCoverage:
    def test_plan_within_evidence(self):
        plan = {"steps": [{"node": "risk_probe"}, {"node": "sink"}]}
        assert plan_covers(plan, ("risk_probe", "sink"))

    def test_plan_referencing_unknown_node_rejected(self):
        plan = {"steps": [{"node": "risk_probe"}, {"node": "ghost"}]}
        assert plan_covers(plan, ("risk_probe", "sink")) is False

    def test_empty_or_malformed_plan_rejected(self):
        assert plan_covers({"steps": []}, ("a",)) is False
        assert plan_covers({}, ("a",)) is False
        assert plan_covers({"steps": [{"node": ""}]}, ("a",)) is False


class TestPlanCacheEnabled:
    def test_default_off(self):
        assert plan_cache_enabled() is False

    def test_on_variants(self, monkeypatch):
        for v in ("1", "true", "ON", "yes"):
            monkeypatch.setenv("FUSION_COWORK_PLAN_CACHE", v)
            assert plan_cache_enabled() is True
        monkeypatch.setenv("FUSION_COWORK_PLAN_CACHE", "0")
        assert plan_cache_enabled() is False


class TestCachedCompile:
    async def test_cache_disabled_never_stores(self):
        calls = []

        async def extract():
            calls.append(1)
            return {"steps": [{"node": "a"}]}

        plan, from_cache = await cached_compile(STEPS, plan_signature(STEPS), extract)
        assert from_cache is False and plan["steps"]
        await cached_compile(STEPS, plan_signature(STEPS), extract)
        assert len(calls) == 2
        assert get_plan_library().size() == 0

    async def test_hit_skips_extract(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_PLAN_CACHE", "1")
        calls = []

        async def extract():
            calls.append(1)
            return {"steps": [{"node": "risk_probe"}, {"node": "sink"}]}

        plan1, from1 = await cached_compile(STEPS, plan_signature(STEPS), extract)
        assert from1 is False and len(calls) == 1
        plan2, from2 = await cached_compile(STEPS, plan_signature(STEPS), extract)
        assert from2 is True and plan2 == plan1 and len(calls) == 1
        lib = get_plan_library()
        assert lib.hits == 1 and lib.misses == 1

    async def test_coverage_gate_forces_fresh_extract(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_PLAN_CACHE", "1")
        lib = get_plan_library()
        lib._plans[("risk_probe", "sink")] = {"steps": [{"node": "ghost"}]}

        async def extract():
            return {"steps": [{"node": "risk_probe"}, {"node": "sink"}]}

        plan, from_cache = await cached_compile(STEPS, plan_signature(STEPS), extract)
        assert from_cache is False
        assert plan_covers(plan, plan_signature(STEPS))

    async def test_store_refuses_empty_plan(self):
        lib = PlanLibrary()
        lib.store(("a",), {"steps": []})
        assert lib.size() == 0
        lib.store(("a",), {"steps": [{"node": "a"}]})
        assert lib.size() == 1


class _CountingStub:
    calls = 0

    async def chat(self, model, messages, temperature=0.0, max_tokens=0, **kw):
        _CountingStub.calls += 1
        return SimpleNamespace(content=json.dumps({"steps": [{"node": "risk_probe"}, {"node": "sink"}]}))


class TestCompileWorkflowIntegration:
    async def test_second_compile_same_steps_pays_zero_llm(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_PLAN_CACHE", "1")
        _CountingStub.calls = 0
        plan1, ids1 = await compile_workflow(STEPS, _CountingStub(), _make_factory(), Workflow(name="one"))
        assert plan1["steps"] and len(ids1) == 2
        assert _CountingStub.calls == 1
        plan2, ids2 = await compile_workflow(STEPS, _CountingStub(), _make_factory(), Workflow(name="two"))
        assert plan2 == plan1 and len(ids2) == 2
        assert _CountingStub.calls == 1

    async def test_default_off_always_extracts(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_PLAN_CACHE", raising=False)
        _CountingStub.calls = 0
        for name in ("one", "two"):
            await compile_workflow(STEPS, _CountingStub(), _make_factory(), Workflow(name=name))
        assert _CountingStub.calls == 2
