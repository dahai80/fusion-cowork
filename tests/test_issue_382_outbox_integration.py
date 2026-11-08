"""issue #382: fusion-harness outbox 内核接线 — 事务性落账 + 幂等写回执 + 启动 reconcile。

- OutboxBridge: 内核缺失/环境开关关闭 → 禁用 (一次响亮警告, 零行为变化)
- 引擎 record_step: 节点结果单事务入 run_events; effects 声明入口 collect_outbox_effects
- execute_idempotent_async: 回执命中跳过重复写; 未命中写后落回执
- GitHubNode issue_create: 引擎 outbox ctx 下 POST/PUT 经回执, 重放不重复写
- recover_persisted: 废弃 run 的 effects 诚实退役 (reconcile)
"""

import asyncio

import httpx
import pytest

from fusion_cowork.engine.node import BaseNode, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.outbox import (
    get_outbox_bridge,
    outbox_ctx,
    register_effect_handler,
    reset_outbox_bridge,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.nodes.ops.ops_nodes import GitHubNode

pytest.importorskip("fusion_harness")


class MockOut382(BaseNode):
    name = "mock_out_382"
    display_name = "Mock Out 382"
    category = "tool"
    description = "mock"
    icon = "x"
    default_label = "mock"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="out382 done")


class MockEffects382(MockOut382):
    name = "mock_effects_382"

    def collect_outbox_effects(self):
        return [
            {
                "node_id": "n1",
                "action": "notify_test",
                "payload": {"msg": "hi"},
            }
        ]


@pytest.fixture()
def _mock_nodes():
    for cls in (MockOut382, MockEffects382):
        register_node(cls)
    yield
    for cls in (MockOut382, MockEffects382):
        NodeRegistry.unregister(cls.name, force=True)


@pytest.fixture()
def outbox_env(tmp_path, monkeypatch):
    db = str(tmp_path / "outbox.db")
    monkeypatch.setenv("FUSION_COWORK_OUTBOX_DB", db)
    monkeypatch.delenv("FUSION_COWORK_OUTBOX", raising=False)
    reset_outbox_bridge()
    yield db
    reset_outbox_bridge()


def _engine_and_run(node_name):
    wf = {
        "name": "t382",
        "nodes": [{"id": "n1", "name": node_name, "params": []}],
        "edges": [],
    }
    return WorkflowEngine(), Workflow.from_dict(wf)


class TestBridgeEnablement:
    def test_enabled_with_kernel(self, outbox_env):
        bridge = get_outbox_bridge()
        assert bridge is not None and bridge.enabled
        assert outbox_env.startswith("/")

    def test_kill_switch_disables(self, outbox_env, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_OUTBOX", "0")
        assert get_outbox_bridge() is None

    def test_disabled_bridge_noop(self, outbox_env, monkeypatch):
        # 内核缺失路径: store=None 的桥一切调用零影响
        monkeypatch.setattr("fusion_cowork.engine.outbox.OutboxStore", None)
        reset_outbox_bridge()
        bridge = get_outbox_bridge()
        assert bridge is None


class TestEngineJournal:
    @pytest.mark.usefixtures("_mock_nodes")
    async def test_node_result_journaled(self, outbox_env):
        bridge = get_outbox_bridge()
        engine, wf = _engine_and_run("mock_out_382")
        res = await engine.execute(wf)
        assert res.status == WorkflowStatus.SUCCESS
        store = bridge._store
        events = store.events_for_run(res.id)
        assert any(e["type"] == "node_result" and e["payload"].get("node_id") == "n1" for e in events)

    @pytest.mark.usefixtures("_mock_nodes")
    async def test_declared_effects_enqueued_deduped(self, outbox_env):
        bridge = get_outbox_bridge()
        engine, wf = _engine_and_run("mock_effects_382")
        res = await engine.execute(wf)
        assert res.status == WorkflowStatus.SUCCESS
        effects = bridge._store.effects_for_run(res.id)
        assert [e.action for e in effects] == ["notify_test"]
        # 同 receipt_key 重跑不重复入队
        res2 = await engine.execute(wf, execution_id=res.id)
        assert res2.status == WorkflowStatus.SUCCESS
        assert len(bridge._store.effects_for_run(res.id)) == 1

    def test_record_step_failure_does_not_raise(self, outbox_env):
        bridge = get_outbox_bridge()
        bridge._store.close()
        # 关闭后的 store 落账失败: 响亮警告, 不抛
        bridge.record_step("run_x", "n1", "success", "s", [])


class TestIdempotentAsync:
    async def test_replay_skips_write(self, outbox_env):
        bridge = get_outbox_bridge()
        calls = []

        async def write():
            calls.append(1)
            return {"number": 7}

        data1, replayed1 = await bridge.execute_idempotent_async("r1:n1:issue_create:t", "github:o/r", write)
        data2, replayed2 = await bridge.execute_idempotent_async("r1:n1:issue_create:t", "github:o/r", write)
        assert replayed1 is False and data1 == {"number": 7}
        assert replayed2 is True and data2 == {"number": 7}
        assert len(calls) == 1

    async def test_receipt_survives_bridge_recreate(self, outbox_env):
        bridge = get_outbox_bridge()
        await bridge.execute_idempotent_async("r2:n1:op", "agg", lambda: asyncio.sleep(0, result={"v": 1}))
        reset_outbox_bridge()
        bridge2 = get_outbox_bridge()
        _, replayed = await bridge2.execute_idempotent_async("r2:n1:op", "agg", lambda: asyncio.sleep(0, result={}))
        assert replayed is True


class TestGithubNodeReceipt:
    def _patch_client(self, monkeypatch, counter):
        monkeypatch.setenv("GITHUB_TOKEN", "t382")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/search/issues":
                return httpx.Response(200, json={"items": []})
            if request.method == "POST" and request.url.path == "/repos/o/r/issues":
                counter.append(1)
                return httpx.Response(201, json={"number": 42, "title": "t", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )

    def _node(self):
        return GitHubNode.__new__(GitHubNode)

    async def test_write_replayed_not_duplicated(self, outbox_env, monkeypatch):
        from fusion_cowork.engine.node import NodeConfig

        counter = []
        self._patch_client(monkeypatch, counter)
        node = self._node()
        node.config = NodeConfig(params={"op": "issue_create", "repo": "o/r", "title": "t", "allowed_repos": ["o/r"]})
        token = outbox_ctx.set({"run_id": "run_r", "node_id": "gh1", "node_name": "gh"})
        try:
            res1 = await node.execute({})
            res2 = await node.execute({})
        finally:
            outbox_ctx.reset(token)
        assert res1.status == NodeStatus.SUCCESS and res2.status == NodeStatus.SUCCESS
        assert len(counter) == 1
        assert res2.data["result"]["number"] == 42

    async def test_no_ctx_writes_directly(self, outbox_env, monkeypatch):
        from fusion_cowork.engine.node import NodeConfig

        counter = []
        self._patch_client(monkeypatch, counter)
        node = self._node()
        node.config = NodeConfig(params={"op": "issue_create", "repo": "o/r", "title": "t", "allowed_repos": ["o/r"]})
        res = await node.execute({})
        assert res.status == NodeStatus.SUCCESS
        assert len(counter) == 1


class TestReconcileAndWorker:
    async def test_reconcile_retires_orphaned_effects(self, outbox_env):
        bridge = get_outbox_bridge()
        store = bridge._store
        store.commit_step("orphan_run", [], [{"node_id": "n1", "action": "notify_test", "payload": {}}])
        report = bridge.reconcile(["orphan_run"])
        assert report.retired_effects == 1
        assert store.effect_by_receipt("orphan_run:n1:notify_test").status == "retired"

    async def test_worker_delivers_effect_with_handler(self, outbox_env):
        delivered = []
        register_effect_handler("notify_test", lambda effect, payload: delivered.append(payload))
        try:
            bridge = get_outbox_bridge()
            bridge._store.commit_step(
                "run_w", [], [{"node_id": "n1", "action": "notify_test", "payload": {"msg": "hi"}}]
            )
            bridge.ensure_worker()
            for _ in range(100):
                if delivered:
                    break
                await asyncio.sleep(0.05)
            assert delivered == [{"msg": "hi"}]
        finally:
            bridge = get_outbox_bridge()
            if bridge is not None:
                await bridge.stop_worker()
            from fusion_cowork.engine import outbox as ob_mod

            ob_mod._EFFECT_HANDLERS.pop("notify_test", None)

    async def test_worker_failure_backs_off(self, outbox_env):
        async def boom(effect, payload):
            raise RuntimeError("no route")

        register_effect_handler("notify_test", boom)
        try:
            bridge = get_outbox_bridge()
            store = bridge._store
            store.commit_step("run_f", [], [{"node_id": "n1", "action": "notify_test", "payload": {}}])
            bridge.ensure_worker()
            for _ in range(100):
                eff = store.effect_by_receipt("run_f:n1:notify_test")
                if eff.status == "pending" and eff.attempts >= 1 and eff.last_error:
                    break
                await asyncio.sleep(0.05)
            eff = store.effect_by_receipt("run_f:n1:notify_test")
            assert eff.status == "pending"
            assert "no route" in (eff.last_error or "")
        finally:
            bridge = get_outbox_bridge()
            if bridge is not None:
                await bridge.stop_worker()
            from fusion_cowork.engine import outbox as ob_mod

            ob_mod._EFFECT_HANDLERS.pop("notify_test", None)


class TestRecoverPersistedReconcile:
    async def test_recover_retires_discarded_run_effects(self, outbox_env, tmp_path, monkeypatch):
        # run_store 返回坏 workflow_def 行 → 废弃 → 其 effects 经 reconcile 退役
        from fusion_cowork.server.web_api import RunRegistry

        bridge = get_outbox_bridge()
        bridge._store.commit_step("bad_run", [], [{"node_id": "n1", "action": "notify_test", "payload": {}}])

        class FakeStore:
            def load_active(self):
                return [
                    {
                        "run_id": "bad_run",
                        "workflow_def": "not-json",
                        "steps": "[]",
                        "directives": "[]",
                        "status": "running",
                    }
                ]

            def delete(self, run_id):
                pass

        runs = RunRegistry.__new__(RunRegistry)
        runs.run_store = FakeStore()
        runs._runs = {}
        runs._tasks = {}
        runs._engine = None
        runs._persist = lambda rid: None
        n = await runs.recover_persisted()
        assert n == 0
        eff = bridge._store.effect_by_receipt("bad_run:n1:notify_test")
        assert eff.status == "retired"
