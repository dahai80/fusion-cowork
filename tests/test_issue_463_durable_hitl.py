"""issue #463: durable HITL — approval decision journal (SQLite), approvals survive crash restart.

Gate-entry pending journaling / decision journaling (incl. timeout auto-deny) /
decision replay after restart / pending rows restored into the approval queue /
web pending queue merge.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.approval_journal import ApprovalJournal
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

HITL_NODE = "hitl_probe_463"


@register_node
class _Probe463(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 463"
    category = NodeCategory.TOOL
    description = "test probe: high-risk node for durable HITL (#463)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _pm(monkeypatch) -> PermissionManager:
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    wf = Workflow(name="hitl-wf-463")
    probe = NodeRegistry.create(HITL_NODE)
    nid = wf.add_node(probe)
    return wf, nid


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


class TestApprovalJournalCRUD:
    def test_record_resolve_decision(self, tmp_path: Path):
        j = ApprovalJournal(tmp_path / "j.db")
        j.record_pending("act-1", "run-1", node_id="n1", tool_name="shell_exec", params={"cmd": "ls"})
        assert len(j.pending()) == 1
        assert j.decision_for("run-1", "n1") is None
        assert j.resolve("act-1", True, decided_by="tester") is True
        # Second resolve is a no-op (only the pending row is touched)
        assert j.resolve("act-1", False, decided_by="x") is False
        row = j.decision_for("run-1", "n1")
        assert row is not None
        assert row["approved"] == 1
        assert row["status"] == "approved"
        assert row["decided_by"] == "tester"
        assert j.pending() == []
        j.close()

    def test_re_record_resets_pending(self, tmp_path: Path):
        j = ApprovalJournal(tmp_path / "j.db")
        j.record_pending("act-1", "run-1")
        j.resolve("act-1", True)
        j.record_pending("act-1", "run-2", node_id="n1")
        assert len(j.pending()) == 1
        assert j.pending()[0]["run_id"] == "run-2"
        j.close()

    def test_resolve_run_batch(self, tmp_path: Path):
        j = ApprovalJournal(tmp_path / "j.db")
        j.record_pending("a1", "run-9", node_id="n1")
        j.record_pending("a2", "run-9", node_id="n2")
        j.record_pending("a3", "run-8", node_id="n3")
        n = j.resolve_run("run-9", False, decided_by="timeout")
        assert n == 2
        assert j.pending_for_run("run-9") == []
        assert len(j.pending_for_run("run-8")) == 1
        j.close()


class TestEngineJournalWiring:
    async def test_gate_records_pending_and_resolve(self, tmp_path: Path, monkeypatch):
        pm = _pm(monkeypatch)
        j = ApprovalJournal(tmp_path / "j.db")
        eng = WorkflowEngine(permission_manager=pm, hitl_pause=True, approval_journal=j)
        exec_id = "run-463-approve"
        wf, nid = _wf()
        task = asyncio.create_task(eng.execute(wf, execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        pend = j.pending_for_run(exec_id)
        assert len(pend) == 1
        assert pend[0]["node_id"] == nid
        assert pend[0]["status"] == "pending"
        # Decision journaled (resolve_hitl internally calls resolve_run); once=True one-shot approval persists no rule
        assert eng.resolve_hitl(exec_id, True, once=True, attribution={"decided_by": "alice"})
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        # issue #507: decision is consumed immediately once adopted by the engine — decision_for no longer
        # returns the used decision, the row stays as status='consumed' (auditable), revision-loop re-asks
        # of the gate are not polluted by replay
        assert j.decision_for(exec_id, nid) is None

    async def test_timeout_denies_journal(self, tmp_path: Path, monkeypatch):
        pm = _pm(monkeypatch)
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "0.2")
        j = ApprovalJournal(tmp_path / "j.db")
        eng = WorkflowEngine(permission_manager=pm, hitl_pause=True, approval_journal=j)
        exec_id = "run-463-timeout"
        wf, nid = _wf()
        execution = await asyncio.wait_for(eng.execute(wf, execution_id=exec_id), timeout=10)
        assert execution.status.value == "failed"
        # issue #507: timeout auto-deny is consumed immediately too
        assert j.decision_for(exec_id, nid) is None

    async def test_replay_after_restart(self, tmp_path: Path, monkeypatch):
        # Restart semantics: same journal, new engine (the old engine's decisions died with the process,
        # only the journal lives on) — gate re-entry (_hitl_await) replays the journaled decision
        # directly instead of waiting
        pm = _pm(monkeypatch)
        j = ApprovalJournal(tmp_path / "j.db")
        node = NodeRegistry.create(HITL_NODE)
        j.record_pending("act-463-r", "run-463-replay", node_id=node.id, tool_name=HITL_NODE)
        j.resolve("act-463-r", True, decided_by="alice")
        eng = WorkflowEngine(permission_manager=pm, hitl_pause=True, approval_journal=j)
        wf, _ = _wf()
        allowed = await eng._hitl_await("run-463-replay", wf, node, {})
        assert allowed is True
        assert "run-463-replay" not in eng._hitl_waiting, "重启重放失败: 仍进等待"  # i18n: user-facing

    async def test_replay_denied_decision(self, tmp_path: Path, monkeypatch):
        pm = _pm(monkeypatch)
        j = ApprovalJournal(tmp_path / "j.db")
        node = NodeRegistry.create(HITL_NODE)
        j.record_pending("act-463-d", "run-463-deny", node_id=node.id, tool_name=HITL_NODE)
        j.resolve("act-463-d", False, decided_by="bob")
        eng = WorkflowEngine(permission_manager=pm, hitl_pause=True, approval_journal=j)
        wf, _ = _wf()
        allowed = await eng._hitl_await("run-463-deny", wf, node, {})
        assert allowed is False

    def test_no_journal_zero_change(self, monkeypatch):
        pm = _pm(monkeypatch)
        eng = WorkflowEngine(permission_manager=pm, hitl_pause=True)
        assert eng._approval_journal is None


class TestPendingRestore:
    def test_restore_pending_details(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        n = pm.restore_pending_details(
            [
                {
                    "action_id": "act-r1",
                    "run_id": "run-r1",
                    "node_id": "n1",
                    "tool_name": "shell_exec",
                    "workflow": "wf",
                    "params": {"cmd": "ls"},
                    "ts": 123.0,
                }
            ]
        )
        assert n == 1
        pend = pm.list_pending()
        entry = next(a for a in pend if a["action_id"] == "act-r1")
        assert entry["source"] == "local"
        assert entry["execution_id"] == "run-r1"
        assert entry["restored"] is True
        assert entry["params"] == {"cmd": "ls"}
        # Idempotent: existing rows are not overwritten
        assert pm.restore_pending_details([{"action_id": "act-r1", "run_id": "run-r1"}]) == 0
        # finder is visible per execution (gate re-entry path of a restored run)
        found = pm.find_pending_by_execution("run-r1")
        assert found is not None and found["action_id"] == "act-r1"

    def test_restore_skips_blank_action_id(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert pm.restore_pending_details([{"run_id": "run-x"}, None]) == 0


class TestWebPendingMerge:
    def test_permissions_pending_merges_journal(self, monkeypatch, tmp_path: Path):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine.events import EventEmitter
        from fusion_cowork.server.web_api import create_web_app

        j = ApprovalJournal(tmp_path / "j.db")
        j.record_pending("act-web-1", "run-web-1", node_id="n1", tool_name="shell_exec", params={"cmd": "ls"})

        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        eng = WorkflowEngine(permission_manager=pm, event_emitter=EventEmitter(), approval_journal=j)
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-463")
        app = create_web_app(engine=eng)
        # issue #572: journal rows for runs without a record are dropped —
        # register the referenced run as active so the merge path is exercised
        app.state.run_registry._runs["run-web-1"] = {"run_id": "run-web-1", "status": "running"}
        with TestClient(app) as client:
            r = client.get("/api/permissions/pending", headers={"Authorization": "Bearer test-key-463"})
            assert r.status_code == 200
            approvals = r.json()["approvals"]
            entry = next(a for a in approvals if a["action_id"] == "act-web-1")
            assert entry["source"] == "journal"
            assert entry["execution_id"] == "run-web-1"
            assert entry["params"] == {"cmd": "ls"}


class TestAuditChainWiring:
    async def test_hitl_decision_enters_audit_chain(self, tmp_path: Path):
        from fusion_cowork.security.audit import AuditLog
        from fusion_cowork.server.web_api import _schedule_hitl_audit
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path / "spaces"))
        await store.initialize()
        audit = AuditLog(store)
        app = types.SimpleNamespace(state=types.SimpleNamespace(audit_log=audit))
        _schedule_hitl_audit(app, "run-audit-1", "act-audit-1", True, "web")
        for _ in range(100):
            rows = await audit._fetch_chain("default")
            if rows:
                break
            await asyncio.sleep(0.02)
        rows = await audit._fetch_chain("default")
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "web"
        assert row["action"] == "hitl.resolve"
        assert row["resource"] == "run/run-audit-1/approval/act-audit-1"
        verdict = await audit.verify_chain("default")
        assert verdict["ok"] is True and verdict["count"] == 1
        await store.close()

    async def test_hitl_decision_without_audit_store_noop(self):
        from fusion_cowork.server.web_api import _schedule_hitl_audit

        app = types.SimpleNamespace(state=types.SimpleNamespace(audit_log=None))
        _schedule_hitl_audit(app, "run-audit-2", "act-audit-2", False, "webhook")  # must not raise
