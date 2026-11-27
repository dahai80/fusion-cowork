"""P3-2 + P4 web migration batch (migration doc §6): W19 guard approvals + W21 cluster panel + W9 delivery review + W18 usage.

Contract:
- GET /api/permissions/pending: list_pending_guard snapshot (empty / with items).
- POST /api/permissions/pending/{aid}/confirm: guard disabled → confirmed=false;
  local rules appended after a successful confirm (approve/deny).
- GET /api/cluster/state: cluster off → cluster_enabled=false empty structure;
  on + seeded data → nodes/vram/total.
- POST /api/space/spaces/{sid}/artifacts/{aid}/review: owner/admin review
  approve/reject, status stored in metadata.review; non-reviewer PermissionError.
- GET /api/usage: space-domain counts + quota fields (default unlimited).
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-p4"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    yield TestClient(app), app


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestGuardApprovals:
    def test_pending_empty(self, client):
        tc, _ = client
        resp = tc.get("/api/permissions/pending", headers=_auth())
        assert resp.status_code == 200
        assert resp.json() == {"approvals": []}

    def test_pending_list_and_confirm_local_rules(self, client):
        tc, app = client
        pm = app.state.permission_manager
        pm._pending_guard_approvals["act-1"] = "ShellExec"
        pm._pending_guard_approvals["act-2"] = "FileMove"
        listed = tc.get("/api/permissions/pending", headers=_auth()).json()["approvals"]
        assert {a["action_id"] for a in listed} == {"act-1", "act-2"}
        # guard not enabled → confirm returns False (fail-closed, no local rule added)
        r1 = tc.post("/api/permissions/pending/act-1/confirm", headers=_auth(), json={"approved": True})
        assert r1.status_code == 200
        assert r1.json()["confirmed"] is False
        assert not any(rule.tool_name == "ShellExec" and rule.allowed for rule in pm.rules)

    def test_local_approve_deny_paths(self, client):
        import asyncio

        tc, app = client
        pm = app.state.permission_manager
        pm.approve("FileCopy")
        pm.deny("DiskCleaner")
        assert asyncio.run(pm.check("FileCopy", "execute")) is True
        assert asyncio.run(pm.check("DiskCleaner", "execute")) is False


class TestClusterPanel:
    def test_cluster_disabled(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        monkeypatch.delenv("FUSION_CLUSTER_ENABLED", raising=False)
        import fusion_cowork.distributed_state as ds

        ds._STORE = None
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
        )
        resp = TestClient(app).get("/api/cluster/state", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_enabled"] is False
        assert body["nodes"] == []

    def test_cluster_enabled_with_nodes(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        monkeypatch.setenv("FUSION_CLUSTER_ENABLED", "1")
        import fusion_cowork.distributed_state as ds
        from fusion_cowork.distributed_state import DistributedStateStore

        # Inject a tmp-path store directly (the default path writes to home, forbidden in tests)
        store = DistributedStateStore(state_path=str(tmp_path / "cluster-state.json"), node_id="node-a")
        ds._STORE = store
        store.heartbeat(host="127.0.0.1", port=11461)
        store.record_vram_allocation("plug-1", 4096)
        try:
            web_api._WEB_DIR = tmp_path / "web"
            app = web_api.create_web_app(
                dist_dir=tmp_path / "no-dist",
                store_root=tmp_path / "workflows",
            )
            resp = TestClient(app).get("/api/cluster/state", headers=_auth())
            assert resp.status_code == 200
            body = resp.json()
            assert body["cluster_enabled"] is True
            assert any(n["node_id"] == "node-a" for n in body["nodes"])
            assert body["total_allocated_mb"] == 4096
        finally:
            ds._STORE = None


def _seed_space_with_artifact(tc):
    """Create a space + artifact, return (space_id, artifact_id)."""
    space = tc.post("/api/space/spaces", headers=_auth(), json={"name": "review-space", "description": "w9"}).json()
    sid = space["id"]
    art = tc.post(
        f"/api/space/spaces/{sid}/artifacts",
        headers=_auth(),
        # i18n: user-facing
        json={"name": "周报.md", "artifact_type": "document", "content": "# 周报"},
    ).json()
    return sid, art["id"]


class TestArtifactReview:
    def test_review_approve_and_reject(self, client):
        import json as _json

        tc, _ = client
        sid, aid = _seed_space_with_artifact(tc)
        r = tc.post(
            f"/api/space/spaces/{sid}/artifacts/{aid}/review",
            headers=_auth(),
            json={"approved": True, "note": "ok"},
        )
        assert r.status_code == 200
        assert r.json()["review"]["status"] == "approved"
        art = tc.get(f"/api/space/spaces/{sid}/artifacts/{aid}", headers=_auth()).json()
        review = _json.loads(art["metadata"])["review"]
        assert review["status"] == "approved"
        assert review["note"] == "ok"
        # Reject — re-review overwrites
        r2 = tc.post(f"/api/space/spaces/{sid}/artifacts/{aid}/review", headers=_auth(), json={"approved": False})
        assert r2.status_code == 200
        art2 = tc.get(f"/api/space/spaces/{sid}/artifacts/{aid}", headers=_auth()).json()
        assert _json.loads(art2["metadata"])["review"]["status"] == "rejected"

    def test_review_not_found(self, client):
        tc, _ = client
        sid, _aid = _seed_space_with_artifact(tc)
        r = tc.post(f"/api/space/spaces/{sid}/artifacts/art_missing/review", headers=_auth(), json={"approved": True})
        assert r.status_code == 404

    def test_review_non_reviewer_denied(self, client):
        import asyncio

        tc, app = client
        sid, aid = _seed_space_with_artifact(tc)
        svc = app.state.artifact_svc
        with pytest.raises(PermissionError):
            asyncio.run(svc.review_artifact(sid, aid, "rando_user", approved=True))


class TestUsageOverview:
    def test_usage_empty_unlimited(self, client):
        tc, _ = client
        resp = tc.get("/api/usage", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "default"
        assert body["usage"] == {"spaces": 0, "messages": 0, "artifacts": 0, "agents": 0}
        assert body["quotas"]["max_spaces"] == -1
        assert body["unlimited"] is True

    def test_usage_counts_after_create(self, client):
        tc, _ = client
        _seed_space_with_artifact(tc)
        body = tc.get("/api/usage", headers=_auth()).json()
        assert body["usage"]["spaces"] >= 1
        assert body["usage"]["artifacts"] >= 1


class TestMobileOverview:
    def test_overview_empty_shape(self, client):
        tc, _ = client
        resp = tc.get("/api/mobile/overview", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["runs"] == []
        assert body["pending_approvals"] == []
        assert body["generated_at"] > 0

    def test_overview_reflects_runs_and_approvals(self, client):
        tc, app = client
        app.state.run_registry._runs["run_x"] = {
            "run_id": "run_x",
            "workflow_id": "wf-1",
            "workflow_name": "wf",
            "status": "running",
            "created_at": 1.0,
            "finished_at": None,
            "error": None,
        }
        app.state.permission_manager._pending_guard_approvals["act-9"] = "ShellExec"
        body = tc.get("/api/mobile/overview", headers=_auth()).json()
        assert [r["run_id"] for r in body["runs"]] == ["run_x"]
        # issue #124: pending merges guard+local, guard entries carry source=guard
        assert body["pending_approvals"] == [{"action_id": "act-9", "tool_name": "ShellExec", "source": "guard"}]


class TestDeliverToDesktop:
    def test_deliver_sync_not_injected(self, client):
        tc, _ = client
        resp = tc.post("/api/deliver", headers=_auth(), json={"run_id": "run_x"})
        assert resp.status_code == 503

    def test_deliver_run_not_found(self, client, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api
        from fusion_cowork.server.sync import CrossDeviceSync

        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "wf2",
            cross_sync=CrossDeviceSync(),
        )
        tc2 = TestClient(app)
        resp = tc2.post("/api/deliver", headers=_auth(), json={"run_id": "run_missing"})
        assert resp.status_code == 404

    def test_deliver_offline_unknown_broadcast(self, client, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api
        from fusion_cowork.server.sync import CrossDeviceSync, Device, DeviceStatus

        sync = CrossDeviceSync()
        # host/port point to a definitely-rejected port — the default localhost:11437 would actually connect
        # if a sync service runs locally, making the test environment-dependent (register_device forces ONLINE)
        sync.register_device(
            Device(
                device_id="phone-2",
                name="iPad",
                device_type="ipad",
                host="127.0.0.1",
                port=1,
                status=DeviceStatus.OFFLINE,
            )
        )

        app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "wf2", cross_sync=sync)
        tc2 = TestClient(app)
        tc2.app.state.run_registry._runs["run_y"] = {
            "run_id": "run_y",
            "workflow_name": "wf",
            "status": "completed",
            "created_at": 1.0,
            "finished_at": None,
            "error": None,
        }
        # Offline device → delivered=False (honest, does not pretend delivery)
        r_off = tc2.post("/api/deliver", headers=_auth(), json={"run_id": "run_y", "target_device_id": "phone-2"})
        assert r_off.status_code == 200
        assert r_off.json()["delivered"] is False
        # Unknown device → False
        r_unknown = tc2.post("/api/deliver", headers=_auth(), json={"run_id": "run_y", "target_device_id": "nope"})
        assert r_unknown.json()["delivered"] is False
        # Broadcast (no online devices, zero connection attempts) → True (broadcast semantics)
        r_b = tc2.post("/api/deliver", headers=_auth(), json={"run_id": "run_y"})
        assert r_b.status_code == 200
        assert r_b.json()["receiver"] == "broadcast"
        assert r_b.json()["delivered"] is True
