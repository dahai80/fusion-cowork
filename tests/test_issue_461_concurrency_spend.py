"""issue #461: 跨 run 并发闸 + per-run 波次并行度 + token/花费归因。

concurrency_key 让不同 key 的 run 交错执行 (fair share), 同 key 排队;
步级 usage 汇总成 run 级 tokens/spend_usd, /api/usage 按 run 与 workflow 聚合。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.run_gate import RunGate


@register_node
class _Slow461(BaseNode):
    name = "slow461"
    display_name = "Slow 461"
    category = NodeCategory.LOGIC
    description = "test probe: sleeping node (#461)"
    icon = "🧪"
    default_label = "Slow"

    async def execute(self, inputs: dict) -> NodeResult:
        await asyncio.sleep(0.4)
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True}, summary="slow")


@register_node
class _Usage461(BaseNode):
    name = "usage461"
    display_name = "Usage 461"
    category = NodeCategory.LOGIC
    description = "test probe: usage-emitting node (#461)"
    icon = "🧪"
    default_label = "Usage"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            summary="usage",
        )


class TestRunGate:
    async def test_disabled_by_default(self):
        gate = RunGate()
        assert gate.enabled is False
        assert gate.saturated("a") is False
        await gate.acquire("a")
        gate.release("a")

    async def test_same_key_serializes(self):
        gate = RunGate(max_per_key=1)
        await gate.acquire("a")
        assert gate.saturated("a") is True
        assert gate.saturated("b") is False
        gate.release("a")
        assert gate.saturated("a") is False

    async def test_distinct_keys_interleave(self):
        gate = RunGate(max_per_key=1)
        await gate.acquire("a")
        await gate.acquire("b")
        assert gate.status()["running_by_key"] == {"a": 1, "b": 1}
        gate.release("a")
        gate.release("b")

    async def test_key_queue_waits_for_slot(self):
        gate = RunGate(max_per_key=1)
        await gate.acquire("a")

        async def later():
            await asyncio.sleep(0.05)
            gate.release("a")

        asyncio.create_task(later())
        await gate.acquire("a")
        gate.release("a")

    async def test_global_cap(self):
        gate = RunGate(max_running=1)
        await gate.acquire("a")
        assert gate.saturated("b") is True
        gate.release("a")
        assert gate.saturated("b") is False


class TestRollup:
    def test_token_rollup_sums_steps(self):
        from fusion_cowork.server.web_api import _token_rollup

        steps = [
            {"tokens": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            {"tokens": None},
            {"tokens": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        assert _token_rollup(steps) == {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}

    def test_token_rollup_empty(self):
        from fusion_cowork.server.web_api import _token_rollup

        assert _token_rollup([])["total_tokens"] == 0
        assert _token_rollup(None)["total_tokens"] == 0

    def test_spend_usd_price_env(self, monkeypatch):
        from fusion_cowork.server.web_api import _spend_usd

        assert _spend_usd(1_000_000) == 0.0
        monkeypatch.setenv("FUSION_COWORK_SPEND_USD_PER_MTOK", "3.0")
        assert _spend_usd(1_000_000) == 3.0
        assert _spend_usd(0) == 0.0
        monkeypatch.setenv("FUSION_COWORK_SPEND_USD_PER_MTOK", "bogus")
        assert _spend_usd(1_000_000) == 0.0


class TestConcurrencyAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k461")
        monkeypatch.setenv("FUSION_COWORK_MAX_RUNNING_PER_KEY", "1")
        monkeypatch.delenv("FUSION_COWORK_MAX_RUNNING_RUNS", raising=False)
        from fusion_cowork.engine import PermissionManager, WorkflowEngine

        eng = WorkflowEngine(permission_manager=PermissionManager())
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as client:
            yield client, {"Authorization": "Bearer k461"}

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

    def test_same_key_queues_distinct_key_interleaves(self, api):
        client, auth = api
        wf = self._wf(client, auth, "slow461")
        r1 = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "a"}, headers=auth).json()
        assert r1["status"] == "running"
        r2 = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "a"}, headers=auth).json()
        assert r2["status"] == "queued"
        r3 = client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": "b"}, headers=auth).json()
        assert r3["status"] == "running"
        d1 = self._wait_terminal(client, auth, r1["run_id"])
        assert d1["status"] == "success", d1
        # key a 槽位释放, 排队 run 翻 running 并跑完
        d2 = self._wait_terminal(client, auth, r2["run_id"])
        assert d2["status"] == "success", d2
        assert d2.get("concurrency_key") == "a"

    def test_no_key_unaffected_when_gate_on(self, api):
        client, auth = api
        wf = self._wf(client, auth, "slow461")
        r1 = client.post("/api/runs", json={"workflow_id": wf}, headers=auth).json()
        r2 = client.post("/api/runs", json={"workflow_id": wf}, headers=auth).json()
        # 缺省 key 相同 → 第二个排队; 闸开启本身不改变语义
        assert r1["status"] == "running"
        assert r2["status"] in ("queued", "running")
        assert self._wait_terminal(client, auth, r1["run_id"])["status"] == "success"
        assert self._wait_terminal(client, auth, r2["run_id"])["status"] == "success"

    def test_concurrency_param_validation(self, api):
        client, auth = api
        wf = self._wf(client, auth, "slow461")
        assert client.post("/api/runs", json={"workflow_id": wf, "concurrency": -1}, headers=auth).status_code == 400
        assert client.post("/api/runs", json={"workflow_id": wf, "concurrency": "4"}, headers=auth).status_code == 400
        assert client.post("/api/runs", json={"workflow_id": wf, "concurrency_key": 5}, headers=auth).status_code == 400

    def test_wave_concurrency_passthrough(self, api):
        client, auth = api
        wf = client.post(
            "/api/workflows",
            json={
                "name": "wave-wf",
                "workflow": {
                    "nodes": [{"id": f"n{i}", "name": "slow461", "config": {}} for i in range(3)],
                    "edges": [],
                },
            },
            headers=auth,
        ).json()["id"]
        detail = self._wait_terminal(
            client,
            auth,
            client.post("/api/runs", json={"workflow_id": wf, "concurrency": 3}, headers=auth).json()["run_id"],
            timeout_s=15.0,
        )
        assert detail["status"] == "success", detail
        assert detail.get("concurrency") == 3


class TestSpendAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import PermissionManager, WorkflowEngine
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k461")
        monkeypatch.setenv("FUSION_COWORK_SPEND_USD_PER_MTOK", "2.0")
        eng = WorkflowEngine(permission_manager=PermissionManager())
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as client:
            yield client, {"Authorization": "Bearer k461"}

    def test_run_detail_tokens_and_spend(self, api):
        client, auth = api
        wf = client.post(
            "/api/workflows",
            json={"name": "u", "workflow": {"nodes": [{"id": "n1", "name": "usage461", "config": {}}], "edges": []}},
            headers=auth,
        ).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf}, headers=auth).json()["run_id"]
        deadline = time.time() + 10
        detail = {}
        while time.time() < deadline:
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail.get("status") not in ("running", "queued"):
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        assert detail["tokens"]["total_tokens"] == 15
        assert detail["spend_usd"] == 15 / 1_000_000 * 2.0

    def test_usage_endpoint_aggregates(self, api):
        client, auth = api
        wf = client.post(
            "/api/workflows",
            json={"name": "agg", "workflow": {"nodes": [{"id": "n1", "name": "usage461", "config": {}}], "edges": []}},
            headers=auth,
        ).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf}, headers=auth).json()["run_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if client.get(f"/api/runs/{run_id}", headers=auth).json().get("status") not in ("running", "queued"):
                break
            time.sleep(0.05)
        usage = client.get("/api/usage", headers=auth).json()
        tu = usage["token_usage"]
        assert tu["total_tokens"] >= 15
        assert tu["runs"] >= 1
        assert usage["token_by_workflow"]["agg"]["total_tokens"] >= 15
