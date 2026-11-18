"""Issue #522 — BudgetTracker kernel-funnel duck-type + workflow.py private reach-through removal.

Contract (fusion-harness funnels.py):
- `_budget_exceeded(budget, graph_id)` calls `budget.is_exceeded(graph_id)` (TypeError fallback to zero-arg).
- LLM funnel budget stage reads `budget.spent_tokens` via getattr WITHOUT calling it.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fusion_cowork.ai.budget import BudgetTracker

_HARNESS_FUNNELS = pytest.importorskip("fusion_harness.funnels")


def _tracker_with_cost(cost_usd, max_budget_usd=1.0, enforce=True):
    pricing = {"default": {"input": cost_usd * 1_000_000.0, "output": 0.0}}
    tracker = BudgetTracker(max_budget_usd=max_budget_usd, enforce=enforce, pricing=pricing)
    tracker.record_usage({"prompt_tokens": 1, "completion_tokens": 0})
    return tracker


class TestFunnelDuckType:
    def test_is_exceeded_true_when_enforced_over_budget(self):
        tracker = _tracker_with_cost(2.0, max_budget_usd=1.0)
        assert tracker.is_exceeded() is True

    def test_is_exceeded_accepts_graph_id_call_form(self):
        tracker = _tracker_with_cost(2.0, max_budget_usd=1.0)
        assert tracker.is_exceeded("graph-1") is True

    def test_is_exceeded_false_when_under_budget(self):
        tracker = _tracker_with_cost(0.5, max_budget_usd=1.0)
        assert tracker.is_exceeded() is False

    def test_is_exceeded_false_when_not_enforcing(self):
        tracker = _tracker_with_cost(5.0, max_budget_usd=1.0, enforce=False)
        assert tracker.is_exceeded() is False

    def test_spent_tokens_attribute_matches_record(self):
        tracker = _tracker_with_cost(0.5)
        assert tracker.spent_tokens == tracker.record.total_tokens

    def test_spent_tokens_is_attribute_not_method(self):
        tracker = _tracker_with_cost(0.5)
        assert not callable(tracker.spent_tokens)


class TestHarnessFunnelConsumer:
    def test_budget_exceeded_helper_sees_exceeded_tracker(self):
        tracker = _tracker_with_cost(2.0, max_budget_usd=1.0)
        assert _HARNESS_FUNNELS._budget_exceeded(tracker) is True

    def test_budget_exceeded_helper_sees_under_budget_tracker(self):
        tracker = _tracker_with_cost(0.5, max_budget_usd=1.0)
        assert _HARNESS_FUNNELS._budget_exceeded(tracker, "g1") is False

    @pytest.mark.asyncio
    async def test_llm_funnel_denies_when_budget_exceeded(self):
        from fusion_harness.funnels import LLMFunnel

        tracker = _tracker_with_cost(2.0, max_budget_usd=1.0)
        funnel = LLMFunnel(budget_ref=tracker)
        ctx = SimpleNamespace(session_id="", graph_id="g1")
        proceed, reason, messages = await funnel.pre_llm(ctx, node_label="n1", model="m", messages=[])
        assert proceed is False
        assert reason == "token budget exceeded"
        assert messages == []

    @pytest.mark.asyncio
    async def test_llm_funnel_admits_under_budget(self):
        from fusion_harness.funnels import LLMFunnel

        tracker = _tracker_with_cost(0.5, max_budget_usd=1.0)
        funnel = LLMFunnel(budget_ref=tracker)
        ctx = SimpleNamespace(session_id="", graph_id="g1")
        proceed, reason, _ = await funnel.pre_llm(ctx, node_label="n1", model="m", messages=["hi"])
        assert proceed is True
        assert reason == ""


class TestWorkflowNoPrivateReachThrough:
    def test_workflow_source_has_no_private_budget_access(self):
        source = Path(__file__).resolve().parent.parent.joinpath("fusion_cowork", "engine", "workflow.py").read_text()
        assert "_budget_tracker._" not in source
        assert "_budget_tracker.is_exceeded()" in source
