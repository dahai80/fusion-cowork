"""issue #191: 运行控制状态持久化 — paused/directives/步骤快照落 SQLite, daemon 重启 (kill -9 模拟) 后唯一恢复路径续跑。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.events import EventEmitter
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.run_persistence import SCHEMA_VERSION, RunStateStore
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry

HITL_NODE = "hitl_probe_191"
LOW_NODE = "low_probe_191"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 191"
    category = NodeCategory.TOOL
    description = "测试用: 高风险探针 (#191)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@register_node
class _LowNode(BaseNode):
    name = LOW_NODE
    display_name = "Low Probe 191"
    category = NodeCategory.TOOL
    description = "测试用: 低风险探针 (#191)"
    icon = "1️⃣"
    default_label = "Low"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"low": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf_def() -> dict:
    # runs_create 传给 registry.start 的是 record["workflow"] — 扁平 nodes/edges 形状
    return {
        "name": "durable-wf-191",
        "nodes": [
            {"id": "n1", "name": LOW_NODE, "config": {}},
            {"id": "n2", "name": HITL_NODE, "config": {}},
        ],
        "edges": [{"source_id": "n1", "target_id": "n2"}],
    }


def _stack(store: RunStateStore, pm: PermissionManager):
    emitter = EventEmitter()
    engine = WorkflowEngine(permission_manager=pm, event_emitter=emitter)
    registry = RunRegistry(engine, event_emitter=emitter, run_store=store)
    return engine, registry


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_terminal(registry: RunRegistry, run_id: str, timeout: float = 5.0) -> str:
    for _ in range(int(timeout * 50)):
        rec = registry.get(run_id)
        status = str(rec.get("status", "")) if rec else ""
        if status not in ("running", "paused"):
            return status
        await asyncio.sleep(0.02)
    return "timeout"


class TestRunStateStore:
    def test_save_load_delete_roundtrip(self, tmp_path):
        store = RunStateStore(tmp_path / "run_states.db")
        record = {
            "run_id": "run_a",
            "workflow_id": "wf_a",
            "workflow_name": "wf-a",
            "status": "paused",
            "paused_reason": "manual",
            "created_at": time.time(),
            "workflow_def": _wf_def(),
            "steps": [{"node_id": "n1", "status": "success"}],
            "directives": [{"action": "skip_remaining", "at": 1.0, "payload": {"action": "skip_remaining"}}],
            "memory": False,
        }
        store.save(record, paused_reason="manual")
        assert store.count() == 1
        rows = store.load_active()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run_a"
        assert rows[0]["paused_reason"] == "manual"
        assert '"node_id": "n1"' in rows[0]["steps"]
        store.delete("run_a")
        assert store.count() == 0
        store.close()

    def test_terminal_rows_are_stale_cleaned_loud(self, tmp_path, caplog):
        store = RunStateStore(tmp_path / "run_states.db")
        store.save({"run_id": "run_b", "status": "running", "created_at": time.time()}, paused_reason="")
        # 伪造终态残留行
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO run_states (run_id, status, created_at) VALUES ('run_old', 'success', 1.0)"
            )
            store._conn.commit()
        with caplog.at_level("WARNING"):
            rows = store.load_active()
        assert [r["run_id"] for r in rows] == ["run_b"]
        assert "终态残留行" in caplog.text
        assert store.count() == 1
        store.close()

    def test_schema_version_gate_loud(self, tmp_path):
        path = tmp_path / "run_states.db"
        store = RunStateStore(path)
        store.close()
        conn = sqlite3.connect(str(path))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="拒绝静默降级读取"):
            RunStateStore(path)


class TestCrashRecovery:
    async def test_hitl_crash_recover_approve_completes(self, pm_no_guard, tmp_path):
        # 上半场: run 在 HITL 边界暂停 (持久化状态 running = kill -9 时刻)
        _ProbeNode.calls = 0
        _LowNode.calls = 0
        db = tmp_path / "run_states.db"
        engine1, reg1 = _stack(RunStateStore(db), pm_no_guard)
        started = reg1.start(_wf_def(), workflow_id="wf_191", workflow_name="durable-wf-191")
        run_id = started["run_id"]
        assert await _wait_hitl(engine1, run_id)
        assert _LowNode.calls == 1
        assert reg1.run_store.count() == 1
        # kill -9: 摘 store + 取消任务
        reg1.run_store = None
        task = reg1._tasks[run_id]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pm_no_guard.discard_pending_by_execution(run_id)
        # 下半场: 新引擎 + 新 registry + 同一 SQLite — 启动恢复
        engine2, reg2 = _stack(RunStateStore(db), pm_no_guard)
        assert await reg2.recover_persisted() == 1
        rec = reg2.get(run_id)
        assert rec is not None
        assert rec["status"] == "paused"
        assert rec["paused_reason"] == "crash"
        assert rec["recovered"] is True
        # 快照含 n1 → 恢复续跑跳过 n1, 直接撞 n2 的 HITL
        assert reg2.resume(run_id) is True
        assert await _wait_hitl(engine2, run_id)
        assert _LowNode.calls == 1, "恢复续跑不应重跑已完成节点"
        # 审批通过 → run 完成; 终态行出库
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        await pm_no_guard.confirm_guard(action_id, approved=True, approved_by="test")
        assert engine2.resolve_hitl(run_id, True)
        status = await _wait_terminal(reg2, run_id)
        assert status == "success", rec
        assert _ProbeNode.calls == 1
        assert reg2.run_store.count() == 0
        reg2.run_store.close()

    async def test_directive_replayed_after_recovery(self, pm_no_guard, tmp_path, caplog):
        import logging

        _ProbeNode.calls = 0
        db = tmp_path / "run_states.db"
        engine1, reg1 = _stack(RunStateStore(db), pm_no_guard)
        run_id = reg1.start(_wf_def(), workflow_id="wf_191")["run_id"]
        assert await _wait_hitl(engine1, run_id)
        # 停机前排队一条改向指令 (全量 payload 落库)
        err = reg1.directive(run_id, {"action": "set_params", "node_id": "n2", "params": {"foo": "bar"}})
        assert err is None
        row = RunStateStore(db).load_active()[0]
        assert '"foo": "bar"' in row["directives"]
        RunStateStore(db).close()
        reg1.run_store = None
        task = reg1._tasks[run_id]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pm_no_guard.discard_pending_by_execution(run_id)
        # 恢复: 指令重放进新引擎队列, resume 后首个节点边界生效
        engine2, reg2 = _stack(RunStateStore(db), pm_no_guard)
        with caplog.at_level(logging.INFO, logger="fusion_cowork.server.web_api"):
            assert await reg2.recover_persisted() == 1
        assert "恢复期间重放改向指令 1 条" in caplog.text
        queued = engine2._directives.get(run_id, [])
        assert len(queued) == 1
        assert queued[0].get("params") == {"foo": "bar"}
        # 收尾: resume → 指令应用 → HITL → 审批 → 完成
        assert reg2.resume(run_id) is True
        assert await _wait_hitl(engine2, run_id)
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        await pm_no_guard.confirm_guard(action_id, approved=True, approved_by="test")
        engine2.resolve_hitl(run_id, True)
        status = await _wait_terminal(reg2, run_id)
        assert status == "success"
        reg2.run_store.close()

    async def test_manual_paused_row_recovers_as_manual(self, pm_no_guard, tmp_path):
        # 手工暂停语义: 持久化状态 paused → 恢复不判 crash, paused_reason 保留
        db = tmp_path / "run_states.db"
        store = RunStateStore(db)
        store.save(
            {
                "run_id": "run_manual_191",
                "workflow_id": "wf_m",
                "workflow_name": "manual-wf",
                "status": "paused",
                "created_at": time.time(),
                "workflow_def": {
                    "name": "manual-wf",
                    "nodes": [{"id": "n1", "name": LOW_NODE, "config": {}}],
                    "edges": [],
                },
                "steps": [
                    {"node_id": "n1", "status": "success", "output_data": {"low": True}, "node_name": LOW_NODE}
                ],
                "directives": [],
                "memory": False,
            },
            paused_reason="manual",
        )
        store.close()
        _LowNode.calls = 0
        engine2, reg2 = _stack(RunStateStore(db), pm_no_guard)
        assert await reg2.recover_persisted() == 1
        rec = reg2.get("run_manual_191")
        assert rec["status"] == "paused"
        assert rec["paused_reason"] == "manual"
        assert reg2.resume("run_manual_191") is True
        status = await _wait_terminal(reg2, "run_manual_191")
        assert status == "success"
        assert _LowNode.calls == 0, "快照完成的节点不应重跑"
        reg2.run_store.close()

    async def test_corrupt_row_discarded_loud(self, pm_no_guard, tmp_path, caplog):
        db = tmp_path / "run_states.db"
        store = RunStateStore(db)
        store.save(
            {
                "run_id": "run_bad_191",
                "workflow_id": "wf_x",
                "workflow_name": "bad",
                "status": "paused",
                "created_at": time.time(),
                "workflow_def": {"nope": 1},  # Workflow.from_dict 解析失败
                "steps": [],
                "directives": [],
                "memory": False,
            },
            paused_reason="manual",
        )
        store.close()
        engine2, reg2 = _stack(RunStateStore(db), pm_no_guard)
        with caplog.at_level("ERROR", logger="fusion_cowork.server.web_api"):
            assert await reg2.recover_persisted() == 0
        assert "run_bad_191" in caplog.text
        assert reg2.run_store.count() == 0, "无法解析的行应被废弃删除"
        reg2.run_store.close()


class TestWebWiring:
    def test_create_web_app_wires_run_store(self, pm_no_guard, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-191")
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        store_root = tmp_path / "store"
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=store_root)
        assert app.state.run_registry.run_store is not None
        assert (store_root / "run_states.db").exists()
        # startup 事件触发恢复 (活跃行为 0 → 不抛错)
        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"
        app.state.run_registry.run_store.close()
