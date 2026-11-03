"""issue #261: 画布模板资产 — 发布/导入/导出/删除 REST + 隔离 templates_dir。"""

from __future__ import annotations

import json

import pytest


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k261")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "nd",
        store_root=tmp_path / "wf",
        space_data_dir=tmp_path / "sp",
        templates_dir=tmp_path / "tpl",
    )
    return TestClient(app), tmp_path / "tpl"


def _wf():
    return {
        "name": "canvas-flow",
        "nodes": [
            {"id": "n1", "name": "file_input", "config": {"params": {"path": "~/Downloads"}}},
            {"id": "n2", "name": "shell_exec", "config": {"params": {"command": "ls"}}},
        ],
        "edges": [{"source_id": "n1", "target_id": "n2"}],
    }


HDR = {"x-api-key": "k261"}


class TestTemplatePublish:
    def test_publish_and_get(self, monkeypatch, tmp_path):
        tc, tdir = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post("/api/templates", json={"name": "My Flow", "workflow": _wf()}, headers=HDR)
            assert r.status_code == 200, r.text
            assert r.json()["id"] == "my-flow"
            r2 = tc.get("/api/templates/my-flow", headers=HDR)
            assert r2.status_code == 200
            assert r2.json()["workflow"]["nodes"][0]["id"] == "n1"
            assert (tdir / "my-flow.json").exists()

    def test_publish_missing_nodes_400(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post("/api/templates", json={"name": "x", "workflow": {"nodes": []}}, headers=HDR)
            assert r.status_code == 400

    def test_publish_slug_collision_overwrites(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            for _ in range(2):
                r = tc.post("/api/templates", json={"name": "My Flow", "workflow": _wf()}, headers=HDR)
                assert r.status_code == 200
            lst = tc.get("/api/templates", headers=HDR).json()
            mine = [t for t in lst["templates"] if t.get("id") == "my-flow"]
            assert len(mine) == 1


class TestTemplateImportExport:
    def test_import_bare_workflow(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post("/api/templates/import", json=_wf(), headers=HDR)
            assert r.status_code == 200
            assert r.json()["id"] == "canvas-flow"

    def test_import_bad_400(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.post("/api/templates/import", json={"no": "nodes"}, headers=HDR)
            assert r.status_code == 400

    def test_export_attachment(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            tc.post("/api/templates", json={"name": "Exp Flow", "workflow": _wf()}, headers=HDR)
            r = tc.get("/api/templates/exp-flow/export", headers=HDR)
            assert r.status_code == 200
            assert "attachment" in r.headers.get("content-disposition", "")
            body = json.loads(r.content)
            assert body["id"] == "exp-flow"
            assert body["workflow"]["edges"][0]["source_id"] == "n1"

    def test_export_missing_404(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            assert tc.get("/api/templates/nope/export", headers=HDR).status_code == 404


class TestTemplateDelete:
    def test_delete_custom(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            tc.post("/api/templates", json={"name": "Del Me", "workflow": _wf()}, headers=HDR)
            r = tc.delete("/api/templates/del-me", headers=HDR)
            assert r.status_code == 200
            assert tc.get("/api/templates/del-me", headers=HDR).status_code == 404

    def test_delete_builtin_refused(self, monkeypatch, tmp_path):
        tc, _ = _client(monkeypatch, tmp_path)
        with tc:
            lst = tc.get("/api/templates", headers=HDR).json()
            builtin = [t for t in lst["templates"] if not str(t.get("id", "")).startswith("custom_")]
            if not builtin:
                pytest.skip("无内置模板")
            r = tc.delete(f"/api/templates/{builtin[0]['id']}", headers=HDR)
            assert r.status_code == 400
