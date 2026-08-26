"""集群模型同步测试 — issue #61 迁移自 fusion-multi-node。

覆盖: manifest 建造/序列化, sync_diff 增量, 分区检测降级/恢复,
SSRF + 路径穿越防护, 负载报告。
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.cluster_sync import (
    ClusterSyncManager,
    FileEntry,
    ModelManifest,
    NodeHealth,
    NodeLoadReport,
    PartitionDetector,
    PartitionState,
    build_manifest,
    build_safe_url,
    compute_file_sha256,
    compute_sync_diff,
    is_safe_path_segment,
    is_safe_peer_host,
)

# ── manifest 建造 + 序列化 ──


def test_manifest_to_dict_from_dict_roundtrip():
    m = ModelManifest(
        model_name="qwen2",
        model_id="abc",
        files=[FileEntry(path="config.json", size=100, sha256="dead", modified_at=1.0)],
        total_size=100,
        created_at=2.0,
    )
    d = m.to_dict()
    assert d["model_name"] == "qwen2"
    assert d["files"][0]["sha256"] == "dead"
    m2 = ModelManifest.from_dict(d)
    assert m2.model_name == "qwen2"
    assert m2.files[0].path == "config.json"
    assert m2.files[0].sha256 == "dead"


def test_build_manifest_missing_dir_returns_empty(tmp_path):
    m = build_manifest("nope", str(tmp_path / "missing"))
    assert m.model_name == "nope"
    assert m.files == []


def test_build_manifest_scans_files(tmp_path):
    model_dir = tmp_path / "qwen"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    (model_dir / "weights.safetensors").write_text("x" * 50)
    m = build_manifest("qwen", str(model_dir))
    paths = {f.path for f in m.files}
    assert paths == {"config.json", "weights.safetensors"}
    assert m.total_size == 52
    assert all(f.sha256 for f in m.files)


def test_compute_file_sha256_known(tmp_path):
    import hashlib

    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert compute_file_sha256(str(p)) == hashlib.sha256(b"hello").hexdigest()


def test_compute_file_sha256_missing_returns_empty():
    assert compute_file_sha256("/nonexistent/path/xyz") == ""


# ── sync diff ──


def test_compute_sync_diff_new_and_changed():
    local = ModelManifest(model_name="m", files=[FileEntry(path="a", sha256="1")])
    remote = ModelManifest(
        model_name="m",
        files=[FileEntry(path="a", sha256="2"), FileEntry(path="b", sha256="3")],
    )
    diff = compute_sync_diff(local, remote)
    paths = {f.path for f in diff}
    assert paths == {"a", "b"}


def test_compute_sync_diff_no_diff():
    local = ModelManifest(model_name="m", files=[FileEntry(path="a", sha256="1")])
    remote = ModelManifest(model_name="m", files=[FileEntry(path="a", sha256="1")])
    assert compute_sync_diff(local, remote) == []


def test_compute_sync_diff_deleted_flag():
    local = ModelManifest(model_name="m", files=[FileEntry(path="a", sha256="1")])
    remote = ModelManifest(model_name="m", files=[])
    diff = compute_sync_diff(local, remote)
    assert len(diff) == 1
    assert diff[0].sha256 == "__deleted__"


# ── SSRF 防护 ──


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "metadata.google.internal",
        "127.0.0.1",
        "localhost",
        "0.0.0.0",
        "::1",
    ],
)
def test_is_safe_peer_host_blocks_restricted(host):
    assert not is_safe_peer_host(host)


def test_is_safe_peer_host_blocks_injection_chars():
    for bad in ["a@b", "a/b", "a?b", "a#b", "", "  "]:
        assert not is_safe_peer_host(bad)


def test_is_safe_peer_host_allows_private_lan():
    assert is_safe_peer_host("192.168.1.10")
    assert is_safe_peer_host("10.0.0.5")


def test_build_safe_url_rejects_bad_host():
    with pytest.raises(ValueError):
        build_safe_url("http", "169.254.169.254", 80, "/x")


def test_build_safe_url_rejects_bad_path():
    with pytest.raises(ValueError):
        build_safe_url("http", "192.168.1.10", 80, "/x/../y")


def test_build_safe_url_ok():
    url = build_safe_url("http", "192.168.1.10", 8080, "/api/models/q/manifest")
    assert url == "http://192.168.1.10:8080/api/models/q/manifest"


# ── 路径穿越防护 ──


@pytest.mark.parametrize("seg", [".", "..", "a/b", "a\\b", "a\x00b", "", "a b"])
def test_is_safe_path_segment_rejects_bad(seg):
    assert not is_safe_path_segment(seg)


def test_is_safe_path_segment_accepts_good():
    for ok in ["config.json", "model.safetensors", "my-model_v1.2", "abc"]:
        assert is_safe_path_segment(ok)


# ── PartitionDetector ──


async def test_partition_detector_timeout_to_degraded():
    det = PartitionDetector("n1", heartbeat_timeout=0.1, check_interval=0.05)
    called = []
    det.register_callbacks(on_partition=lambda d: called.append(d))
    det.update_heartbeat("peer_a")
    await det.start()
    await asyncio.sleep(0.3)
    await det.stop()
    assert det.is_degraded
    assert det.state == PartitionState.PARTITIONED
    assert called and "peer_a" in called[0]


async def test_partition_detector_reconnect():
    det = PartitionDetector("n1", heartbeat_timeout=0.2, check_interval=0.05)
    reconnected = []
    det.register_callbacks(on_reconnect=lambda: reconnected.append(1))
    det.update_heartbeat("peer_a")
    await det.start()
    await asyncio.sleep(0.3)
    assert det.is_degraded
    det.update_heartbeat("peer_a")
    await asyncio.sleep(0.05)
    await det.stop()
    assert not det.is_degraded
    assert det.state == PartitionState.CONNECTED
    assert reconnected


async def test_partition_detector_get_status():
    det = PartitionDetector("n1", heartbeat_timeout=30.0)
    det.update_heartbeat("peer_a")
    status = det.get_status()
    assert status["node_id"] == "n1"
    assert status["partition_state"] == "connected"
    assert "peer_a" in status["nodes"]
    assert status["nodes"]["peer_a"]["status"] == "connected"


# ── ClusterSyncManager ──


async def test_sync_manager_start_stop(tmp_path):
    mgr = ClusterSyncManager(model_cache_dir=str(tmp_path), node_id="n1")
    await mgr.start()
    assert mgr._running
    await mgr.stop()
    assert not mgr._running


def test_sync_manager_get_manifest_empty(tmp_path):
    mgr = ClusterSyncManager(model_cache_dir=str(tmp_path), node_id="n1")
    m = mgr.get_manifest("nope")
    assert m.model_name == "nope"
    assert m.files == []


def test_sync_manager_incremental_sync_no_diff(tmp_path):
    mgr = ClusterSyncManager(model_cache_dir=str(tmp_path), node_id="n1")
    remote = ModelManifest(model_name="m", files=[])
    result = asyncio.run(mgr.incremental_sync("m", remote, source_host="192.168.1.10"))
    assert result["status"] == "up_to_date"
    assert result["synced"] == 0


async def test_sync_manager_incremental_sync_rejects_ssrf(tmp_path):
    mgr = ClusterSyncManager(model_cache_dir=str(tmp_path), node_id="n1")
    remote = ModelManifest(model_name="m", files=[FileEntry(path="a", sha256="1")])
    result = await mgr.incremental_sync("m", remote, source_host="169.254.169.254")
    assert result["synced"] == 0


async def test_sync_manager_trigger_sync_rejects_ssrf(tmp_path):
    mgr = ClusterSyncManager(model_cache_dir=str(tmp_path), node_id="n1")
    mgr.trigger_sync("m", source_host="169.254.169.254")
    await mgr.start()
    await asyncio.sleep(0.2)
    await mgr.stop()


def test_sync_manager_collect_load_report():
    pytest.importorskip("psutil")
    mgr = ClusterSyncManager(node_id="n1")
    report = mgr.collect_load_report()
    assert isinstance(report, NodeLoadReport)
    assert report.node_id == "n1"
    assert report.reported_at > 0


def test_node_health_values():
    assert NodeHealth.HEALTHY == "healthy"
    assert NodeHealth.DEGRADED == "degraded"


# ── lazy import 注册 ──


def test_lazy_import_cluster_sync_symbols():
    import fusion_cowork as fc

    for name in [
        "ClusterSyncManager",
        "ModelManifest",
        "FileEntry",
        "PartitionState",
        "PartitionDetector",
        "is_safe_peer_host",
    ]:
        assert hasattr(fc, name), f"lazy import missing: {name}"
