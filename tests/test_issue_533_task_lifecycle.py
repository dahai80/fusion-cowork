"""#533 K4 Phase B item 4 — task lifecycle converged onto kernel TeamTask
state machine topology.

Ruling (see fusion_cowork/orchestrator/task_lifecycle.py header): the cowork
transition table is DERIVED from fusion_harness.team_runtime._TASK_TRANSITIONS
(core statuses renamed) plus documented product extensions (failed/skipped,
rework reopen). Kernel TeamRuntime stays the team entity; AgentTask stays the
product record. Illegal transitions raise TaskTransitionError.
"""

import pytest

from fusion_cowork.orchestrator.task_lifecycle import (
    TASK_TRANSITIONS,
    TERMINAL_STATUSES,
    TaskTransitionError,
    claim_task,
    transition_task,
)


class _T:
    def __init__(self, status="pending", task_id="t1"):
        self.status = status
        self.task_id = task_id


class TestKernelDerivation:
    def test_core_kernel_edges_present(self):
        # kernel todo->in_progress->done maps to pending->running->completed
        assert ("pending", "running") in TASK_TRANSITIONS
        assert ("running", "completed") in TASK_TRANSITIONS
        assert ("running", "cancelled") in TASK_TRANSITIONS

    def test_kernel_queued_blocked_edges_dropped(self):
        # kernel scheduling states have no cowork status counterpart
        for src, dst in TASK_TRANSITIONS:
            assert (src, dst) not in {("pending", "pending"), ("running", "pending")}

    def test_kernel_table_unchanged(self):
        from fusion_harness import team_runtime

        assert "todo" in team_runtime._TASK_STATUSES
        assert ("todo", "in_progress") in team_runtime._TASK_TRANSITIONS
        assert "failed" not in team_runtime._TASK_STATUSES  # cowork extension


class TestProductExtensions:
    def test_failed_and_skipped_are_cowork_extensions(self):
        assert ("pending", "failed") in TASK_TRANSITIONS
        assert ("pending", "skipped") in TASK_TRANSITIONS
        assert ("running", "failed") in TASK_TRANSITIONS
        assert ("failed", "pending") in TASK_TRANSITIONS  # rework reopen
        assert ("completed", "pending") in TASK_TRANSITIONS  # rejection on completed task

    def test_terminal_statuses(self):
        assert set(TERMINAL_STATUSES) == {"completed", "failed", "cancelled", "skipped"}


class TestTransitionGuard:
    def test_legal_transition_applies(self):
        t = _T("running")
        assert transition_task(t, "completed") == "completed"
        assert t.status == "completed"

    def test_illegal_transition_raises(self):
        t = _T("completed")
        with pytest.raises(TaskTransitionError):
            transition_task(t, "running")
        assert t.status == "completed"  # untouched on refusal

    def test_idempotent_same_status_noop(self):
        t = _T("failed")
        assert transition_task(t, "failed") == "failed"
        assert t.status == "failed"

    def test_rework_reopen_legal(self):
        t = _T("failed")
        transition_task(t, "pending")
        assert t.status == "pending"

    def test_skipped_to_cancelled_legal(self):
        t = _T("skipped")
        transition_task(t, "cancelled")
        assert t.status == "cancelled"


class TestClaim:
    def test_first_claim_wins(self):
        t = _T()
        assert claim_task(t, "agent_a") is True
        assert claim_task(t, "agent_b") is False
        assert claim_task(t, "agent_a") is True  # re-claim by same claimant

    def test_unclaimed_then_claimed(self):
        t = _T()
        assert getattr(t, "_claimed_by", "") == ""
        assert claim_task(t, "agent_a") is True
        assert t._claimed_by == "agent_a"
