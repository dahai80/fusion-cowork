"""#124: 本地 CONFIRM 拒绝进入 Web 审批队列 — guard OFF 也能 GUI 批准。

Contract:
- guard OFF 时高风险节点 check() 拒绝, 同时 _pending_local_approvals 记录;
- list_pending() 合并 guard + local (source 字段区分);
- confirm_guard(action_id, True) 对本地条目加 approve 规则 → 后续 check() 放行;
- confirm_guard(action_id, False) 加 deny 规则;
- reset() 清空本地待审批。
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager


@pytest.fixture
def pm_no_guard(monkeypatch):
    # guard 关闭 (默认): FUSION_GUARD_ENABLED 不设 + socket 不存在
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    return PermissionManager(level=PermissionLevel.CONFIRM)


class TestLocalPendingQueue:
    async def test_high_risk_denied_and_queued(self, pm_no_guard):
        assert await pm_no_guard.check("shell_exec", "execute") is False
        pending = pm_no_guard.list_pending()
        assert len(pending) == 1
        assert pending[0]["source"] == "local"
        assert pending[0]["tool_name"] == "shell_exec"
        assert pending[0]["action_id"].startswith("local_")

    async def test_confirm_approves_and_unblocks(self, pm_no_guard):
        await pm_no_guard.check("shell_exec", "execute")
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        ok = await pm_no_guard.confirm_guard(action_id, approved=True, approved_by="test")
        assert ok is True
        assert pm_no_guard.list_pending() == []
        assert any(r.tool_name == "shell_exec" and r.allowed for r in pm_no_guard.rules)
        assert await pm_no_guard.check("shell_exec", "execute") is True

    async def test_confirm_rejects_adds_deny_rule(self, pm_no_guard):
        await pm_no_guard.check("disk_cleaner", "execute")
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        assert await pm_no_guard.confirm_guard(action_id, approved=False) is True
        assert pm_no_guard.list_pending() == []
        assert await pm_no_guard.check("disk_cleaner", "execute") is False

    async def test_list_pending_merges_guard_entries(self, pm_no_guard):
        await pm_no_guard.check("shell_exec", "execute")
        pm_no_guard._pending_guard_approvals["act-g1"] = "FileMove"
        merged = pm_no_guard.list_pending()
        sources = {e["source"] for e in merged}
        assert sources == {"local", "guard"}
        # 旧接口仍只回 guard (向后兼容)
        assert {e["action_id"] for e in pm_no_guard.list_pending_guard()} == {"act-g1"}

    async def test_reset_clears_local_pending(self, pm_no_guard):
        await pm_no_guard.check("shell_exec", "execute")
        assert len(pm_no_guard.list_pending()) == 1
        pm_no_guard.reset()
        assert pm_no_guard.list_pending() == []
