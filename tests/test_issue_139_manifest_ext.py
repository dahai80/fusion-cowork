"""Issue #139 回归测试 — manifest ext + 搜索/通知源。

manifest 遵循 fusion-web-shell#24 ext schema; /api/search 聚合工作流/运行/空间;
通知源 id 稳定, 满足宿主 dedup 合同。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MANIFEST = WEB_DIR / "manifest.json"

STATIC_KEY = "test-key-139"

VALID_KINDS = {"approvals", "runs", "engine"}


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
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestManifestSchema:
    def test_pages_deep_links_valid(self):
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        pages = m.get("pages") or []
        assert pages, "manifest 应声明 pages"
        for p in pages:
            route = p["route"]
            assert route.startswith("/") and "#" not in route and ".." not in route
            assert p.get("title")
            assert len(pages) <= 32

    def test_search_provider_endpoint(self):
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        sps = m.get("searchProviders") or []
        assert sps, "manifest 应声明 searchProviders"
        for sp in sps:
            assert sp["id"] and sp["label"]
            ep = sp["endpoint"]
            assert ep.startswith("/shell/fusion-cowork/")
            assert ".." not in ep
            assert len(sps) <= 32

    def test_notifications_sources_valid(self):
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        notifs = m.get("notifications") or []
        assert notifs, "manifest 应声明 notifications"
        for n in notifs:
            assert n["id"] and n["label"]
            ep = n["endpoint"]
            assert ep.startswith("/shell/fusion-cowork/")
            assert ".." not in ep
            assert n["kind"] in VALID_KINDS
            assert len(notifs) <= 32

    def test_kinds_mapping(self):
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        kinds = {n["id"]: n["kind"] for n in m["notifications"]}
        assert kinds == {"approvals": "approvals", "runs": "runs"}


def _mk_wf(client, name):
    r = client.post(
        "/api/workflows",
        json={"name": name, "description": f"desc {name}", "workflow": {"name": name, "nodes": {}, "edges": []}},
        headers=_auth(),
    )
    r.raise_for_status()
    return r.json()["id"]


def _mk_space(client, name):
    r = client.post("/api/space/spaces", headers=_auth(), json={"name": name, "description": ""})
    r.raise_for_status()
    return r.json()["id"]


class TestSearch:
    def test_workflow_hit(self, client):
        wf_id = _mk_wf(client, "整理下载文件夹")
        r = client.get("/api/search", params={"q": "下载"}, headers=_auth())
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(i["route"] == f"/canvas/{wf_id}" and "整理下载文件夹" in i["title"] for i in items)

    def test_empty_q_empty_items(self, client):
        r = client.get("/api/search", params={"q": ""}, headers=_auth())
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_no_match_empty(self, client):
        r = client.get("/api/search", params={"q": "zzz-nothing-here"}, headers=_auth())
        assert r.json()["items"] == []

    def test_case_insensitive_run_hit(self, client):
        r = client.post(
            "/api/runs",
            json={
                "name": "TidyDownloadsRun",
                "workflow": {
                    "name": "TidyDownloadsRun",
                    "nodes": {"n1": {"name": "nonexistent_type"}},
                    "edges": [],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 202
        deadline = time.time() + 5
        while time.time() < deadline:
            rr = client.get("/api/search", params={"q": "tidydownloads"}, headers=_auth())
            if any("运行: " in i["title"] for i in rr.json()["items"]):
                break
            time.sleep(0.1)
        items = rr.json()["items"]
        assert any(i["kind"] == "run" and "TidyDownloadsRun" in i["title"] for i in items)

    def test_space_hit(self, client):
        _mk_space(client, "调研空间")
        r = client.get("/api/search", params={"q": "调研"}, headers=_auth())
        items = r.json()["items"]
        assert any(i["kind"] == "space" and "调研" in i["title"] for i in items)


class TestNotifications:
    def _wait_terminal(self, client):
        r = client.post(
            "/api/runs",
            json={
                "name": "notify-run",
                "workflow": {
                    "name": "notify-run",
                    "nodes": {"n1": {"name": "nonexistent_type"}},
                    "edges": [],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            rr = client.get(f"/api/runs/{run_id}", headers=_auth())
            if rr.json().get("status") in ("completed", "failed", "canceled"):
                return rr.json()
            time.sleep(0.1)
        raise AssertionError("run 未在 5s 内到达终态")

    def test_runs_source_failed_run_level_error(self, client):
        rec = self._wait_terminal(client)
        r = client.get("/api/notifications/runs", headers=_auth())
        items = r.json()["items"]
        hit = next(i for i in items if i["id"] == rec["run_id"])
        assert "notify-run" in hit["title"]
        assert hit["level"] == "error"
        assert hit["route"] == "/runs"

    def test_runs_source_ids_stable_across_polls(self, client):
        self._wait_terminal(client)
        first = client.get("/api/notifications/runs", headers=_auth()).json()["items"]
        second = client.get("/api/notifications/runs", headers=_auth()).json()["items"]
        assert [i["id"] for i in first] == [i["id"] for i in second]

    def test_runs_source_cap_32(self, client):
        for n in range(34):
            client.post(
                "/api/runs",
                json={
                    "name": f"cap-{n}",
                    "workflow": {"name": f"cap-{n}", "nodes": {"n1": {"name": "nonexistent_type"}}, "edges": []},
                },
                headers=_auth(),
            )
        deadline = time.time() + 10
        while time.time() < deadline:
            items = client.get("/api/notifications/runs", headers=_auth()).json()["items"]
            if len(items) >= 32:
                break
        assert len(items) <= 32

    def test_approvals_source_empty_ok(self, client):
        items = client.get("/api/notifications/approvals", headers=_auth()).json()["items"]
        assert items == []

    def test_approvals_source_pending_item(self, client):
        pm = client.app.state.permission_manager
        pm._pending_local_approvals["perm_test139"] = "shell_exec"
        try:
            r = client.get("/api/notifications/approvals", headers=_auth())
            items = r.json()["items"]
        finally:
            pm._pending_local_approvals.pop("perm_test139", None)
        hit = next(i for i in items if i["id"] == "perm_test139")
        assert "shell_exec" in hit["title"]
        assert hit["level"] == "warning"
        assert hit["route"] == "/approvals"

    def test_approvals_ids_stable_across_polls(self, client):
        pm = client.app.state.permission_manager
        pm._pending_local_approvals["perm_stable"] = "shell_exec"
        try:
            first = client.get("/api/notifications/approvals", headers=_auth()).json()["items"]
            second = client.get("/api/notifications/approvals", headers=_auth()).json()["items"]
        finally:
            pm._pending_local_approvals.pop("perm_stable", None)
        assert [i["id"] for i in first] == [i["id"] for i in second]
