"""issue #340: gate decisions never persist as rules — deny/approve rules must not pre-deny or blindly allow approval_gate.

A gate decision is a workflow decision (feeding the on_reject=revise revision loop), not a tool-level permission:
a pre-denying deny rule would let one denial permanently kill all later gate HITL cards; a blindly allowing
approve rule equals permanently skipping the human gate. check() skips rule matching for gates, confirm only dequeues.
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.permission_manager import HITL_GATE_TOOLS


@pytest.fixture
def pm_340(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    return PermissionManager(level=PermissionLevel.CONFIRM)


class TestGateRuleExempt:
    async def test_deny_rule_does_not_preempt_gate(self, pm_340):
        # An approval_gate deny rule left by one denial must not silently pre-deny — a pending approval card must still be produced
        pm_340.deny("approval_gate")
        assert any(not r.allowed and r.tool_name == "approval_gate" for r in pm_340.rules)
        allowed = await pm_340.check("approval_gate", "execute", {})
        assert allowed is False
        # Key: not deny-fast but enqueue into pending approvals (HITL card visible)
        pending = [p for p in pm_340.list_pending() if p.get("tool_name") == "approval_gate"]
        assert len(pending) == 1
        assert pending[0]["source"] == "local"

    async def test_approve_rule_does_not_blind_pass_gate(self, pm_340):
        # A historical approve rule must not blindly allow the gate — the human gate requires approval every time
        pm_340.approve("approval_gate")
        allowed = await pm_340.check("approval_gate", "execute", {})
        assert allowed is False
        assert pm_340.list_pending()

    async def test_confirm_on_gate_persists_no_rule(self, pm_340):
        await pm_340.check("approval_gate", "execute", {})
        action_id = pm_340.list_pending()[0]["action_id"]
        assert await pm_340.confirm_guard(action_id, approved=True, approved_by="t") is True
        assert await pm_340.confirm_guard("local_missing", approved=False, approved_by="t") is False
        # After dequeue no approval_gate rule is persisted (neither approve nor deny)
        assert not any(r.tool_name == "approval_gate" for r in pm_340.rules)

    async def test_gate_tools_constant(self):
        assert "approval_gate" in HITL_GATE_TOOLS

    async def test_non_gate_tools_keep_rule_semantics(self, pm_340):
        # Regression: non-gate tool deny rules still pre-deny (no longer enqueued), approve rules still allow
        pm_340.deny("shell_exec")
        assert await pm_340.check("shell_exec", "execute", {}) is False
        assert not any(p.get("tool_name") == "shell_exec" for p in pm_340.list_pending())
        pm_340.approve("python_repl")
        assert await pm_340.check("python_repl", "execute", {}) is True
