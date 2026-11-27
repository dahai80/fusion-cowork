"""issue #251: system workspace directory enumeration — new directory names must be picked from real server-side directories, no free-typed input."""

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
        # i18n: user-facing
        (root / "Q3 对账").mkdir(parents=True)
        (root / "alpha").mkdir()
        (root / "zeta").mkdir()
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        assert body["root"] == str(root)
        # i18n: user-facing
        assert [d["name"] for d in body["directories"]] == sorted(["Q3 对账", "alpha", "zeta"], key=str.lower)
        assert all(d["path"] for d in body["directories"])

    def test_excludes_files_dotfiles_and_deep_dirs(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot2"
        (root / "real").mkdir(parents=True)
        (root / ".hidden").mkdir()
        (root / "plain.txt").write_text("x", encoding="utf-8")
        (root / "real" / "nested").mkdir()  # only immediate subdirectories are listed
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs", headers=_auth())
        names = [d["name"] for d in r.json()["directories"]]
        assert names == ["real"]

    def test_subpath_browse_descends(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot4"
        (root / "proj" / "src" / "deep").mkdir(parents=True)
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs?path=proj", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["relative"] == "proj"
        assert [d["name"] for d in body["directories"]] == ["src"]
        assert body["directories"][0]["relative"] == "proj/src"
        r2 = tc.get("/api/workspaces/system-dirs?path=proj/src", headers=_auth())
        b2 = r2.json()
        assert b2["relative"] == "proj/src"
        assert [d["name"] for d in b2["directories"]] == ["deep"]
        r3 = tc.get("/api/workspaces/system-dirs?path=proj/src/deep", headers=_auth())
        assert r3.json()["directories"] == []
        assert r3.json()["relative"] == "proj/src/deep"

    def test_path_escape_is_400(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot5"
        root.mkdir()
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        for evil in ("../etc", "..", "a/../../b"):
            r = tc.get(f"/api/workspaces/system-dirs?path={evil}", headers=_auth())
            assert r.status_code == 400, (evil, r.text)
            assert r.json()["detail"]["code"] == "bad_path"
        # Absolute paths are normalized to root-relative names → out-of-root enumeration is impossible (400/404 are both safe, 200 strictly forbidden)
        r = tc.get("/api/workspaces/system-dirs?path=/etc", headers=_auth())
        assert r.status_code in (400, 404)

    def test_missing_subdir_is_404(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot6"
        root.mkdir()
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs?path=ghost", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "not_found"

    def test_subpath_dir_is_file_404(self, client, monkeypatch, tmp_path):
        tc, _ = client
        root = tmp_path / "wsroot7"
        root.mkdir()
        (root / "plain.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(root))
        r = tc.get("/api/workspaces/system-dirs?path=plain.txt", headers=_auth())
        assert r.status_code == 404

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
        assert r.json()["available"] is True  # the test machine's home must be a directory

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
