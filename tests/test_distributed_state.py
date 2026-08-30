"""分布式状态层测试 (issue #79) — mock, 无 live 集群。

覆盖:
- DistributedStateStore 原子落盘/读回/损坏重建
- heartbeat + 节点存活判定 + 超时剔除
- vRAM 账本: 记录/释放/集群汇总/限额检查
- 插件状态跨集群可见
- ClusterNodeRegistry / ClusterTaskScheduler 包装合并 peer + 故障转移
- opt-in OFF: get_cluster_state_store() None, 零行为变化
- 清理 temp 状态文件
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from fusion_cowork.distributed_state import (
    ClusterNode,
    ClusterNodeRegistry,
    ClusterState,
    ClusterTaskScheduler,
    DistributedStateStore,
    VramAllocation,
    is_cluster_enabled,
    reset_cluster_state_store,
)


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "cluster-state.json")


@pytest.fixture
def store(state_path):
    s = DistributedStateStore(state_path=state_path, node_id="test-node-1")
    yield s
    if os.path.exists(state_path):
        os.unlink(state_path)


@pytest.fixture(autouse=True)
def _reset_store_singleton(monkeypatch):
    monkeypatch.delenv("FUSION_CLUSTER_ENABLED", raising=False)
    reset_cluster_state_store()
    yield
    reset_cluster_state_store()


class TestStateSerialization:
    def test_save_and_load_roundtrip(self, store, state_path):
        store.heartbeat(
            host="10.0.0.1", port=11452, role="coordinator", vram_total_mb=65536, vram_used_mb=4096, tags=["gpu"]
        )
        fresh = DistributedStateStore(state_path=state_path, node_id="test-node-1")
        fresh.invalidate_cache()
        nodes = fresh.list_all_nodes()
        assert len(nodes) == 1
        assert nodes[0].host == "10.0.0.1"
        assert nodes[0].vram_total_mb == 65536
        assert nodes[0].tags == ["gpu"]

    def test_corrupt_file_rebuilds_empty(self, store, state_path):
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as f:
            f.write("{not valid json")
        fresh = DistributedStateStore(state_path=state_path, node_id="fresh")
        assert fresh.list_all_nodes() == []

    def test_atomic_write_no_partial(self, store, state_path):
        store.heartbeat(host="h1")
        with open(state_path) as f:
            data = json.load(f)
        assert "nodes" in data
        assert data["version"] >= 1

    def test_version_increments_each_write(self, store):
        store.heartbeat(host="h")
        v1 = store.get_state_snapshot().version
        store.heartbeat(host="h2")
        v2 = store.get_state_snapshot().version
        assert v2 > v1


class TestHeartbeatAndLiveness:
    def test_heartbeat_registers_node(self, store):
        store.heartbeat(host="10.0.0.2")
        nodes = store.list_all_nodes()
        assert any(n.node_id == "test-node-1" for n in nodes)

    def test_dead_node_filtered_from_alive(self, store):
        store.heartbeat(host="h")
        state = store.get_state_snapshot()
        state.nodes["test-node-1"].last_heartbeat_at = time.time() - 999.0
        with open(store.state_path, "w") as f:
            json.dump(state.to_dict(), f)
        store.invalidate_cache()
        assert store.list_nodes() == []
        assert len(store.list_all_nodes()) == 1

    def test_peer_nodes_excludes_self(self, store):
        store.heartbeat(host="self-host")
        peers = store.get_peer_nodes()
        assert all(p.node_id != "test-node-1" for p in peers)


class TestVramLedger:
    def test_record_and_release(self, store):
        store.record_vram_allocation("plugin-a", 2048)
        assert store.total_vram_allocated_mb() == 2048
        store.record_vram_allocation("plugin-a", 4096)
        assert store.total_vram_allocated_mb() == 4096
        store.release_vram_allocation("plugin-a")
        assert store.total_vram_allocated_mb() == 0

    def test_cluster_usage_aggregates_by_node(self, state_path):
        s1 = DistributedStateStore(state_path=state_path, node_id="n1")
        s1.record_vram_allocation("p1", 1000)
        s2 = DistributedStateStore(state_path=state_path, node_id="n2")
        s2.record_vram_allocation("p1", 2000)
        s2.invalidate_cache()
        usage = s2.cluster_vram_usage()
        assert usage.get("n1") == 1000
        assert usage.get("n2") == 2000

    def test_can_allocate_respects_limit(self, store):
        store.record_vram_allocation("p1", 4000)
        assert store.can_allocate_vram("test-node-1", 1000, 5000) is True
        assert store.can_allocate_vram("test-node-1", 2000, 5000) is False

    def test_zero_limit_unlimited(self, store):
        store.record_vram_allocation("p1", 99999)
        assert store.can_allocate_vram("test-node-1", 99999, 0) is True


class TestPluginStateSync:
    def test_record_and_query(self, store):
        store.record_plugin_state("plugin-x", installed=True, enabled=True)
        states = store.plugin_state_across_cluster("plugin-x")
        assert len(states) == 1
        assert states[0].enabled is True

    def test_enabled_anywhere(self, store):
        assert store.is_plugin_enabled_anywhere("p") is False
        store.record_plugin_state("p", installed=True, enabled=True)
        assert store.is_plugin_enabled_anywhere("p") is True

    def test_cross_node_visibility(self, state_path):
        s1 = DistributedStateStore(state_path=state_path, node_id="n1")
        s1.record_plugin_state("p", installed=True, enabled=True)
        s2 = DistributedStateStore(state_path=state_path, node_id="n2")
        s2.invalidate_cache()
        assert s2.is_plugin_enabled_anywhere("p") is True


class TestClusterNodeRegistry:
    def test_list_merges_local_and_peers(self, store):
        store.heartbeat(host="self", port=1)
        s2 = DistributedStateStore(state_path=store.state_path, node_id="peer-1")
        s2.heartbeat(host="10.0.0.9", port=11452, vram_total_mb=32768, tags=["gpu"])
        store.invalidate_cache()

        class FakeLocal:
            def list(self, category=None):
                return [{"name": "local_node", "category": "tool"}]

            def get(self, name):
                return {"name": name} if name == "local_node" else None

        reg = ClusterNodeRegistry(FakeLocal(), store)
        merged = reg.list()
        names = [n["name"] for n in merged]
        assert "local_node" in names
        assert "peer:peer-1" in names

    def test_resolve_local_first(self, store):
        class FakeLocal:
            def list(self, category=None):
                return []

            def get(self, name):
                return "LOCAL_MATCH" if name == "local" else None

        reg = ClusterNodeRegistry(FakeLocal(), store)
        assert reg.resolve_node("local") == "LOCAL_MATCH"

    def test_resolve_peer(self, store):
        s2 = DistributedStateStore(state_path=store.state_path, node_id="peer-x")
        s2.heartbeat(host="h")
        store.invalidate_cache()

        class FakeLocal:
            def list(self, category=None):
                return []

            def get(self, name):
                return None

        reg = ClusterNodeRegistry(FakeLocal(), store)
        node = reg.resolve_node("peer:peer-x")
        assert node is not None
        assert node.node_id == "peer-x"

    def test_passthrough_unknown_attr(self, store):
        class FakeLocal:
            custom_attr = "hello"

            def list(self, category=None):
                return []

        reg = ClusterNodeRegistry(FakeLocal(), store)
        assert reg.custom_attr == "hello"


class TestClusterTaskScheduler:
    def test_select_node_prefers_capacity(self, store):
        s_gpu = DistributedStateStore(state_path=store.state_path, node_id="gpu-node")
        s_gpu.heartbeat(host="h", vram_total_mb=65536, vram_used_mb=1024, tags=["gpu"])
        s_small = DistributedStateStore(state_path=store.state_path, node_id="small-node")
        s_small.heartbeat(host="h2", vram_total_mb=8192, vram_used_mb=1024)
        store.invalidate_cache()

        class FakeSched:
            def list_active_tasks(self):
                return []

        sched = ClusterTaskScheduler(FakeSched(), store)
        chosen = sched.select_node_for_dispatch(vram_required_mb=2048, prefer_tags=["gpu"])
        assert chosen == "gpu-node"

    def test_select_node_falls_back_self_when_no_peers(self, store):
        class FakeSched:
            def list_active_tasks(self):
                return []

        sched = ClusterTaskScheduler(FakeSched(), store)
        assert sched.select_node_for_dispatch() == store.node_id

    def test_failover_cycles_candidates(self, store):
        calls = []
        s_peer = DistributedStateStore(state_path=store.state_path, node_id="peer-backup")
        s_peer.heartbeat(host="h", vram_total_mb=32768)
        store.invalidate_cache()

        def executor(node_id):
            calls.append(node_id)
            if node_id == "test-node-1":
                raise RuntimeError("primary down")
            return f"ok-{node_id}"

        class FakeSched:
            def list_active_tasks(self):
                return []

        sched = ClusterTaskScheduler(FakeSched(), store)
        result = sched.dispatch_with_failover(executor)
        assert result["dispatched_to"] is not None
        assert result["result"].startswith("ok-")
        assert len(calls) >= 2

    def test_failover_all_fail(self, store):
        def executor(node_id):
            raise RuntimeError("all down")

        class FakeSched:
            def list_active_tasks(self):
                return []

        sched = ClusterTaskScheduler(FakeSched(), store)
        result = sched.dispatch_with_failover(executor)
        assert result["dispatched_to"] is None
        assert len(result["errors"]) >= 1


class TestOptInOff:
    def test_disabled_returns_none(self):
        assert is_cluster_enabled() is False
        from fusion_cowork.distributed_state import get_cluster_state_store

        assert get_cluster_state_store() is None

    def test_enabled_returns_store(self, monkeypatch):
        monkeypatch.setenv("FUSION_CLUSTER_ENABLED", "1")
        from fusion_cowork.distributed_state import get_cluster_state_store

        store = get_cluster_state_store()
        assert store is not None


class TestDataclassRoundtrip:
    def test_cluster_node_roundtrip(self):
        n = ClusterNode(node_id="x", host="h", port=9, vram_total_mb=100, tags=["a"])
        n2 = ClusterNode.from_dict(n.to_dict())
        assert n2.node_id == "x"
        assert n2.tags == ["a"]

    def test_cluster_state_roundtrip(self):
        s = ClusterState()
        s.nodes["n"] = ClusterNode(node_id="n", host="h")
        s.vram_allocations.append(VramAllocation(node_id="n", plugin_id="p", mb=10))
        s2 = ClusterState.from_dict(s.to_dict())
        assert "n" in s2.nodes
        assert s2.vram_allocations[0].mb == 10


class TestRemoveNode:
    def test_remove_clears_node_state(self, store):
        store.heartbeat(host="h")
        store.record_vram_allocation("p", 100)
        store.record_plugin_state("p", True, True)
        store.remove_node("test-node-1")
        assert store.list_all_nodes() == []
        assert store.total_vram_allocated_mb() == 0
        assert store.is_plugin_enabled_anywhere("p") is False


class TestAsyncHeartbeat:
    def test_heartbeat_async_writes(self, store):
        asyncio.run(store.heartbeat_async(host="async-host"))
        nodes = store.list_all_nodes()
        assert any(n.host == "async-host" for n in nodes)
