# issue #594: RunGate admission-time reservation — runs_start reserves the
# slot synchronously (try_reserve) so a same-key run is never admitted as
# running while an earlier same-key run is pending its _execute. Counters
# replace lazy semaphores; probe and reserve are atomic on the event loop.

import asyncio
import time

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node


@register_node
class _Slow594(BaseNode):
    # same shape as #461's probe node — own class avoids test-order coupling
    name = "slow594"
    display_name = "Slow 594"
    category = NodeCategory.LOGIC
    description = "test probe: sleeping node (#594)"
    icon = "test"
    default_label = "Slow"

    async def execute(self, inputs: dict) -> NodeResult:
        await asyncio.sleep(0.4)
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True})


class TestRunGateReservation:
    def test_try_reserve_per_key_atomic(self):
        from fusion_cowork.engine.run_gate import RunGate

        gate = RunGate(max_per_key=1)
        assert gate.enabled is True
        assert gate.try_reserve("a") is True
        assert gate.try_reserve("a") is False  # same key at cap
        assert gate.saturated("a") is True
        assert gate.try_reserve("b") is True  # distinct key unaffected
        gate.release("a")
        assert gate.try_reserve("a") is True

    def test_try_reserve_global_cap(self):
        from fusion_cowork.engine.run_gate import RunGate

        gate = RunGate(max_running=1)
        assert gate.try_reserve("a") is True
        assert gate.try_reserve("b") is False  # global cap binds across keys
        gate.release("a")
        assert gate.try_reserve("b") is True

    def test_acquire_parity_with_reserve(self):
        # acquire() must consume the same counter pool as try_reserve
        import asyncio

        from fusion_cowork.engine.run_gate import RunGate

        async def main():
            gate = RunGate(max_per_key=1)
            await gate.acquire("a")
            assert gate.try_reserve("a") is False
            gate.release("a")
            assert gate.try_reserve("a") is True
            gate.release("a")
            assert gate.status()["running_by_key"] == {"a": 0}

        asyncio.run(main())

    def test_release_clamps_at_zero(self):
        from fusion_cowork.engine.run_gate import RunGate

        gate = RunGate(max_per_key=2)
        gate.release("a")  # never reserved
        assert gate.status()["running_by_key"] == {"a": 0}
        gate.try_reserve("a")
        gate.release("a")
        gate.release("a")  # double release must not go negative
        assert gate.status()["running_by_key"] == {"a": 0}

    def test_reserve_wait_path_after_release(self):
        # queued run waits in acquire_reserved until release frees the slot
        import asyncio

        from fusion_cowork.engine.run_gate import RunGate

        async def main():
            gate = RunGate(max_per_key=1)
            assert gate.try_reserve("a") is True
            wait_task = asyncio.ensure_future(gate.acquire_reserved("a"))
            await asyncio.sleep(0.1)
            assert not wait_task.done()  # still waiting — slot held
            gate.release("a")
            await asyncio.wait_for(wait_task, timeout=2)
            assert gate.status()["running_by_key"] == {"a": 1}

        asyncio.run(main())

    def test_disabled_gate_reserve_always_false(self):
        from fusion_cowork.engine.run_gate import RunGate

        gate = RunGate()
        assert gate.enabled is False
        assert gate.try_reserve("a") is False
        assert gate.saturated("a") is False


class TestAdmissionAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.server import web_api

        # order-independence: other test files clear the registry without
        # restore (e.g. test_issue_579) — re-register the probe node defensively
        if NodeRegistry.get("slow594") is None:
            NodeRegistry.register(_Slow594)
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k594")
        monkeypatch.setenv("FUSION_COWORK_MAX_RUNNING_PER_KEY", "1")
        monkeypatch.delenv("FUSION_COWORK_MAX_RUNNING_RUNS", raising=False)
        from fusion_cowork.engine import PermissionManager, WorkflowEngine

        eng = WorkflowEngine(permission_manager=PermissionManager())
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as client:
            yield client, {"Authorization": "Bearer k594"}

    def _wf(self, client, auth, node):
        return client.post(
            "/api/workflows",
            json={"name": f"wf-{node}", "workflow": {"nodes": [{"id": "n1", "name": node, "config": {}}], "edges": []}},
            headers=auth,
        ).json()["id"]

    def _wait_terminal(self, client, auth, run_id, timeout_s=10.0):
        deadline = time.time() + timeout_s
        detail = {}
        while time.time() < deadline:
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail.get("status") not in ("running", "queued"):
                return detail
            time.sleep(0.05)
        return detail

    def test_burst_same_key_all_queued_except_first(self, api):
        # the #594 race: N same-key runs created back-to-back — exactly one
        # running (the reservation is admission-atomic), the rest queued;
        # all five drain to terminal success FIFO
        client, auth = api
        wf = self._wf(client, auth, "slow594")
        run_ids = []
        statuses = []
        for _ in range(5):
            r = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "a"}, headers=auth).json()
            run_ids.append(r["run_id"])
            statuses.append(r["status"])
        assert statuses[0] == "running"
        assert statuses[1:] == ["queued"] * 4
        # drain: every queued run turns running then succeeds, one at a time
        for rid in run_ids:
            d = self._wait_terminal(client, auth, rid)
            assert d["status"] == "success", d

    def test_reserve_released_on_completion(self, api):
        # after the run finishes, the key admits a new run immediately
        client, auth = api
        wf = self._wf(client, auth, "slow594")
        r1 = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "a"}, headers=auth).json()
        assert self._wait_terminal(client, auth, r1["run_id"])["status"] == "success"
        r2 = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "a"}, headers=auth).json()
        assert r2["status"] == "running"  # slot was released with the run
        assert self._wait_terminal(client, auth, r2["run_id"])["status"] == "success"
