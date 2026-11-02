"""issue #255: markdown skill 包 CRUD — 用户目录新建/编辑/删除, 内置包只读。"""

from __future__ import annotations

import pytest

STATIC_KEY = "test-key-255"

PACK_MD = """---
name: my-check
command: /my-check
title: 自检
description: 测试用技能包
version: "1.0"
category: custom
---

按步骤执行自检。
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api
    from fusion_cowork.skills import packages as pkgs

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")
    monkeypatch.setattr(pkgs, "USER_PACKAGES_DIR", tmp_path / "user-skills")
    monkeypatch.setattr(pkgs, "_registry", None)
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    yield TestClient(app), tmp_path
    pkgs._registry = None


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestSkillCrud:
    def test_save_creates_user_pack_and_lists(self, client, monkeypatch, tmp_path):
        tc, _ = client
        r = tc.put("/api/skills", headers=_auth(), json={"markdown": PACK_MD})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["saved"] is True
        assert body["name"] == "/my-check"
        assert body["editable"] is True
        assert (tmp_path / "user-skills" / "my-check.md").exists()
        listed = tc.get("/api/skills", headers=_auth()).json()
        names = [s["name"] for s in listed["skills"]]
        assert "/my-check" in names
        editable = {s["name"]: s["editable"] for s in listed["skills"]}
        assert editable["/my-check"] is True

    def test_builtin_packs_readonly_flag(self, client):
        tc, _ = client
        listed = tc.get("/api/skills", headers=_auth()).json()
        builtins = [s for s in listed["skills"] if s["name"] == "/contract-review"]
        assert builtins and builtins[0]["editable"] is False

    def test_source_roundtrip_user_pack(self, client):
        tc, _ = client
        tc.put("/api/skills", headers=_auth(), json={"markdown": PACK_MD})
        r = tc.get("/api/skills/my-check/source", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["editable"] is True
        assert "name: my-check" in body["markdown"]

    def test_source_builtin_not_editable(self, client):
        tc, _ = client
        r = tc.get("/api/skills/contract-review/source", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["editable"] is False
        assert body["markdown"] == ""

    def test_edit_updates_existing_pack(self, client, monkeypatch, tmp_path):
        tc, _ = client
        tc.put("/api/skills", headers=_auth(), json={"markdown": PACK_MD})
        updated = PACK_MD.replace("description: 测试用技能包", "description: 已更新描述")
        r = tc.put("/api/skills", headers=_auth(), json={"markdown": updated})
        assert r.status_code == 200, r.text
        files = list((tmp_path / "user-skills").glob("*.md"))
        assert len(files) == 1
        src = tc.get("/api/skills/my-check/source", headers=_auth()).json()
        assert "已更新描述" in src["markdown"]

    def test_delete_user_pack(self, client, monkeypatch, tmp_path):
        tc, _ = client
        tc.put("/api/skills", headers=_auth(), json={"markdown": PACK_MD})
        r = tc.delete("/api/skills/my-check", headers=_auth())
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        listed = tc.get("/api/skills", headers=_auth()).json()
        assert "/my-check" not in [s["name"] for s in listed["skills"]]

    def test_delete_builtin_refused(self, client):
        tc, _ = client
        r = tc.delete("/api/skills/contract-review", headers=_auth())
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "builtin"

    def test_invalid_markdown_400(self, client):
        tc, _ = client
        r = tc.put("/api/skills", headers=_auth(), json={"markdown": "no frontmatter here"})
        assert r.status_code == 400
        assert r.json()["problems"]

    def test_missing_markdown_400(self, client):
        tc, _ = client
        r = tc.put("/api/skills", headers=_auth(), json={})
        assert r.status_code == 400

    def test_delete_missing_404(self, client):
        tc, _ = client
        r = tc.delete("/api/skills/ghost-pack", headers=_auth())
        assert r.status_code == 404

    def test_requires_auth(self, client):
        tc, _ = client
        assert tc.get("/api/skills").status_code == 401
        assert tc.put("/api/skills", json={"markdown": PACK_MD}).status_code == 401
