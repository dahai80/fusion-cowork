"""issue #340: 闸门决定不落持久规则 — deny/approve 规则不得预拒/盲放 approval_gate.

闸门决定是工作流决策 (喂 on_reject=revise 修订回路), 不是工具级权限:
deny 规则预拒会让一次拒绝永久杀死后续所有闸门 HITL 卡; approve 规则盲放
等于永久跳过人工闸门。check() 对闸门跳过规则匹配, confirm 对闸门只出队。
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
        # 一次拒绝留下的 approval_gate deny 规则不得静默预拒 — 必须照常产生待审批卡
        pm_340.deny("approval_gate")
        assert any(not r.allowed and r.tool_name == "approval_gate" for r in pm_340.rules)
        allowed = await pm_340.check("approval_gate", "execute", {})
        assert allowed is False
        # 关键: 不是 deny-fast, 而是入待审批队列 (HITL 卡可见)
        pending = [p for p in pm_340.list_pending() if p.get("tool_name") == "approval_gate"]
        assert len(pending) == 1
        assert pending[0]["source"] == "local"

    async def test_approve_rule_does_not_blind_pass_gate(self, pm_340):
        # 历史 approve 规则不得盲放闸门 — 人工闸门每次都要走审批
        pm_340.approve("approval_gate")
        allowed = await pm_340.check("approval_gate", "execute", {})
        assert allowed is False
        assert pm_340.list_pending()

    async def test_confirm_on_gate_persists_no_rule(self, pm_340):
        await pm_340.check("approval_gate", "execute", {})
        action_id = pm_340.list_pending()[0]["action_id"]
        assert await pm_340.confirm_guard(action_id, approved=True, approved_by="t") is True
        assert await pm_340.confirm_guard("local_missing", approved=False, approved_by="t") is False
        # 出队后不落任何 approval_gate 规则 (approve/deny 均不落)
        assert not any(r.tool_name == "approval_gate" for r in pm_340.rules)

    async def test_gate_tools_constant(self):
        assert "approval_gate" in HITL_GATE_TOOLS

    async def test_non_gate_tools_keep_rule_semantics(self, pm_340):
        # 回归: 非闸门工具 deny 规则仍预拒 (不再入队), approve 规则仍放行
        pm_340.deny("shell_exec")
        assert await pm_340.check("shell_exec", "execute", {}) is False
        assert not any(p.get("tool_name") == "shell_exec" for p in pm_340.list_pending())
        pm_340.approve("python_repl")
        assert await pm_340.check("python_repl", "execute", {}) is True
