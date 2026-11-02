"""issue #251: 系统工作目录枚举 — 新建目录名只能从服务端真实目录选, 不收手输。"""

from __future__ import annotations

import pytest

STATIC_KEY = "test-key-251"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app), tmp_path


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestSystemDirs:
    def test_lists_immediate_subdirs_sorted(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot"
        (root / "Q3 对账").mkdir(parents=True)
        (root / "alpha").mkdir()
        (root / "zeta").mkdir()
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        assert body["root"] == str(root)
        assert [d["name"] for d in body["directories"]] == sorted(["Q3 对账", "alpha", "zeta"], key=str.lower)
        assert all(d["path"] for d in body["directories"])

    def test_excludes_files_dotfiles_and_deep_dirs(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot2"
        (root / "real").mkdir(parents=True)
        (root / ".hidden").mkdir()
        (root / "plain.txt").write_text("x", encoding="utf-8")
        (root / "real" / "nested").mkdir()  # 只列直接子目录
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        names = [d["name"] for d in r.json()["directories"]]
        assert names == ["real"]

    def test_missing_root_reports_unavailable(self, client, monkeypatch, tmp_path):
        tc, _ = client
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path / "nope"))
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False
        assert body["directories"] == []

    def test_default_root_is_home(self, client, monkeypatch):
        tc, _ = client
        monkeypatch.delenv("FUSION_COWORK_WORKSPACE_ROOT", raising=False)
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        assert r.status_code == 200, r.text
        assert r.json()["available"] is True  # 测试机 home 必为目录

    def test_cap_200(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot3"
        root.mkdir()
        for i in range(230):
            (root / f"d{i:03d}").mkdir()
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        body = tc.get("/api/workspaces/system-dirs", headers=_auth()).json()
        assert len(body["directories"]) == 200

    def test_requires_auth(self, client, monkeypatch, tmp_path):
        tc, _ = client
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path))
        r = tc.get("/api/workspaces/system-dirs")
        assert r.status_code == 401
