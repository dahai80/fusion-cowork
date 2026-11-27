# issue #579: steer/interrupt dual delivery mode + admission-order invariant
# + at-least-once finish notifications.
#
# - directives carry mode: "steer" (default — waits for the next node boundary,
#   in-flight node completes) vs "interrupt" (in-flight attempts are voided,
#   an audit marker step is recorded, directives apply at that boundary in
#   FIFO admission order, voided nodes re-run in the next wave)
# - admission order: an interrupt never overtakes an older queued directive —
#   both apply in queue order at the interrupt boundary
# - ReliableEgress: persist-before-ack durable outbox; retry until 2xx;
#   duplicates possible after a crash between delivery and mark (documented)

import asyncio
import json

import pytest

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import WorkflowEngine, WorkflowStatus

SAVED_REGISTRY = dict(NodeRegistry._registry)


def _register_probe_node(name, sleep_s):
    # each attempt appends (inputs snapshot) to cls.attempts; voided attempts
    # still record "started" because the append happens before the sleep
    async def _execute(self, inputs):
        _execute.attempts.append(dict(inputs or {}))
        await asyncio.sleep(sleep_s)
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")

    _execute.attempts = []
    cls = type(
        f"_Probe579_{name}",
        (BaseNode,),
        {
            "name": name,
            "display_name": name,
            "category": NodeCategory.TOOL,
            "description": "test-only probe node",
            "icon": "x",
            "default_label": name,
            "execute": _execute,
        },
    )
    register_node(cls)
    return cls


def _wf(*node_names):
    from fusion_cowork.engine.workflow import Workflow

    wf = Workflow(name="wf-579")
    ids = {}
    for n in node_names:
        ids[n] = wf.add_node(NodeRegistry.create(n))
    wf.connect(ids[node_names[0]], ids[node_names[-1]]) if len(node_names) > 1 else None
    return wf, ids


@pytest.fixture(autouse=True)
def _restore_registry():
    yield
    NodeRegistry._registry.clear()
    NodeRegistry._registry.update(SAVED_REGISTRY)


class TestDirectiveMode:
    async def test_invalid_mode_rejected(self, tmp_path):
        probe = _register_probe_node("m579_bad", 0.5)
        eng = WorkflowEngine(permission_manager=None)
        exec_id = "run-579-bad"
        wf, ids = _wf(probe.name)
        task = asyncio.ensure_future(eng.execute(wf, execution_id=exec_id))
        await asyncio.sleep(0.05)
        ok = eng.add_directive(
            exec_id, {"action": "inject", "instruction": "fix it", "node_id": ids[probe.name], "mode": "bogus"}
        )
        await task
        assert ok is False

    async def test_steer_default_waits_for_boundary(self, tmp_path):
        probe = _register_probe_node("m579_steer", 0.3)
        follow = _register_probe_node("m579_steer_next", 0.01)
        eng = WorkflowEngine(permission_manager=None)
        exec_id = "run-579-steer"
        wf, ids = _wf(probe.name, follow.name)
        task = asyncio.ensure_future(eng.execute(wf, execution_id=exec_id))
        await asyncio.sleep(0.1)
        # queued mid-flight — steer waits, in-flight node completes untouched
        ok = eng.add_directive(exec_id, {"action": "inject", "instruction": "steer me", "node_id": ids[follow.name]})
        execution = await task
        assert ok is True
        assert execution.status == WorkflowStatus.SUCCESS
        assert len(probe.execute.attempts) == 1, "steer must not void the in-flight node"
        assert follow.execute.attempts[0].get("user_instruction") == "steer me"


class TestInterrupt:
    async def test_interrupt_voids_inflight_and_reruns(self, tmp_path):
        probe = _register_probe_node("m579_int", 0.6)
        eng = WorkflowEngine(permission_manager=None)
        exec_id = "run-579-int"
        wf, ids = _wf(probe.name)
        task = asyncio.ensure_future(eng.execute(wf, execution_id=exec_id))
        await asyncio.sleep(0.15)
        ok = eng.add_directive(
            exec_id,
            {"action": "inject", "instruction": "stop and fix", "node_id": ids[probe.name], "mode": "interrupt"},
        )
        execution = await task
        assert ok is True
        assert execution.status == WorkflowStatus.SUCCESS
        assert len(probe.execute.attempts) == 2, "voided attempt + re-run after directive applied"
        # voided attempt never saw the instruction; re-run did
        assert "user_instruction" not in probe.execute.attempts[0]
        assert probe.execute.attempts[1].get("user_instruction") == "stop and fix"
        markers = [
            s for s in execution.steps if s.user_correction and "用户中断" in (s.summary or "")  # i18n: user-facing
        ]  # i18n: user-facing
        assert markers, "interrupt must record an audit marker step"
        assert eng._interrupt_events.get(exec_id) is None, "interrupt event consumed at boundary"

    async def test_interrupt_never_overtakes_older_directives(self, tmp_path):
        probe = _register_probe_node("m579_order", 0.6)
        eng = WorkflowEngine(permission_manager=None)
        exec_id = "run-579-order"
        wf, ids = _wf(probe.name)
        task = asyncio.ensure_future(eng.execute(wf, execution_id=exec_id))
        await asyncio.sleep(0.15)
        first = eng.add_directive(
            exec_id, {"action": "inject", "instruction": "older", "node_id": ids[probe.name], "merge": {"older": 1}}
        )
        second = eng.add_directive(
            exec_id,
            {
                "action": "inject",
                "instruction": "newer",
                "node_id": ids[probe.name],
                "merge": {"newer": 1},
                "mode": "interrupt",
            },
        )
        execution = await task
        assert first is True and second is True
        assert execution.status == WorkflowStatus.SUCCESS
        # admission order: the interrupt applied after the older steer —
        # both effects present on the re-run (older directive not dropped)
        final = probe.execute.attempts[-1]
        assert final.get("older") == 1 and final.get("newer") == 1


class TestReliableEgress:
    def _fake_egress(self, ok=True):
        class _E:
            def __init__(self):
                self.delivered = []

            async def push_event(self, kind, payload):
                if not ok:
                    return False
                self.delivered.append((kind, payload))
                return True

        return _E()

    def test_enqueue_persists_before_delivery(self, tmp_path):
        from fusion_cowork.server.push import ReliableEgress

        e = self._fake_egress()
        path = str(tmp_path / "outbox.json")
        rel = ReliableEgress(e, path=path)
        assert rel.enqueue("run_finished", {"run_id": "r1"}) is True
        assert e.delivered == [], "no delivery attempt during enqueue"
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 1 and data[0]["kind"] == "run_finished" and data[0]["attempts"] == 0

    async def test_flush_success_removes_record(self, tmp_path):
        from fusion_cowork.server.push import ReliableEgress

        e = self._fake_egress()
        path = str(tmp_path / "outbox.json")
        rel = ReliableEgress(e, path=path)
        rel.enqueue("node_finished", {"run_id": "r1", "node_id": "n1"})
        delivered = await rel.flush_pending()
        assert delivered == 1
        assert rel.pending_count() == 0
        with open(path) as f:
            assert json.load(f) == []

    async def test_flush_failure_keeps_and_counts_attempts(self, tmp_path):
        from fusion_cowork.server.push import ReliableEgress

        e = self._fake_egress(ok=False)
        path = str(tmp_path / "outbox.json")
        rel = ReliableEgress(e, path=path)
        rel.enqueue("run_finished", {"run_id": "r1"})
        assert await rel.flush_pending() == 0
        assert rel.pending_count() == 1
        with open(path) as f:
            assert json.load(f)[0]["attempts"] == 1

    async def test_crash_window_duplicates_documented(self, tmp_path, monkeypatch):
        # delivery succeeds but the mark (save) is lost to a crash — the
        # record survives on disk and the next flush delivers it again
        from fusion_cowork.server.push import ReliableEgress

        e = self._fake_egress()
        path = str(tmp_path / "outbox.json")
        rel = ReliableEgress(e, path=path)
        rel.enqueue("run_finished", {"run_id": "r1"})
        monkeypatch.setattr(rel, "_save", lambda: (_ for _ in ()).throw(OSError("crash before mark")))
        assert await rel.flush_pending() == 1
        monkeypatch.undo()
        rel2 = ReliableEgress(e, path=path)
        assert rel2.pending_count() == 1, "crash window: record still on disk"
        assert await rel2.flush_pending() == 1, "duplicate delivery (documented)"
        assert len(e.delivered) == 2

    def test_from_env_matrix(self, monkeypatch, tmp_path):
        from fusion_cowork.server.push import ReliableEgress

        monkeypatch.delenv("FUSION_COWORK_PUSH_AT_LEAST_ONCE", raising=False)
        monkeypatch.delenv("FUSION_COWORK_PUSH_WEBHOOK_URL", raising=False)
        assert ReliableEgress.from_env() is None
        monkeypatch.setenv("FUSION_COWORK_PUSH_AT_LEAST_ONCE", "1")
        assert ReliableEgress.from_env() is None, "webhook URL required"
        monkeypatch.setenv("FUSION_COWORK_PUSH_WEBHOOK_URL", "http://127.0.0.1:9/hook")
        monkeypatch.setenv("FUSION_COWORK_PUSH_OUTBOX_PATH", str(tmp_path / "ob.json"))
        rel = ReliableEgress.from_env()
        assert rel is not None and rel.egress.enabled


class TestRegistryReliablePush:
    async def _run_and_wait(self, registry, run_id, timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            rec = registry.get(run_id)
            if rec and rec["status"] not in ("running", "paused"):
                return rec
            await asyncio.sleep(0.05)
        return registry.get(run_id)

    async def test_run_finished_and_node_end_enqueued(self, tmp_path):
        from fusion_cowork.server.push import ReliableEgress
        from fusion_cowork.server.web_api import RunRegistry

        probe = _register_probe_node("m579_reg", 0.01)

        class _FakeRelay:
            def __init__(self):
                self.delivered = []

            async def push_event(self, kind, payload):
                self.delivered.append((kind, payload))
                return True

        relay = _FakeRelay()
        from fusion_cowork.engine.events import EventEmitter

        emitter = EventEmitter()
        registry = RunRegistry(WorkflowEngine(event_emitter=emitter), event_emitter=emitter)
        registry.reliable_push = ReliableEgress(relay, path=str(tmp_path / "ob.json"))
        wf = {"name": "wf", "nodes": [{"id": "n1", "name": probe.name}], "edges": []}
        started = registry.start(wf)
        await self._run_and_wait(registry, started["run_id"])
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            kinds = [k for k, _ in relay.delivered]
            if "run_finished" in kinds and "node_finished" in kinds:
                break
            await asyncio.sleep(0.05)
        kinds = [k for k, _ in relay.delivered]
        assert "node_finished" in kinds
        assert "run_finished" in kinds
        node_payload = next(p for k, p in relay.delivered if k == "node_finished")
        assert node_payload["run_id"] == started["run_id"]
        assert node_payload["node_id"] == "n1"
        assert node_payload["status"] == "success"
