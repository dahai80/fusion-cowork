"""Issue #170 回归测试 — Extensions 控制台 (Skills + Knowledge, f-ecommerce E2 迁移)。

Skills: JSON 用户包 校验/安装/注册 SkillRegistry/测试运行/移除; 脚本包仅静态管理密钥。
Knowledge: fusion-rag 代理 — health/KB 列表(过滤+分页)/创建接线/wire/documents/upload/search。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

STATIC_KEY = "test-key-170"

NODE_PACK = {
    "name": "echo-pack",
    "description": "echo node pack",
    "type": "node",
    "node": "shell_exec",
}


class _FakeResp:
    def __init__(self, data, status_code=200):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"upstream {self.status_code}")


class _FakeHttp:
    def __init__(self, state):
        self.state = state

    async def get(self, path: str):
        if path == "/health":
            return _FakeResp({"status": "ok", "version": "0.8.4"})
        if path == "/kb/bases":
            return _FakeResp(self.state["bases"])
        if path.endswith("/documents"):
            kb = path.split("/")[-2]
            return _FakeResp(self.state["docs"].get(kb, []))
        return _FakeResp({}, 404)

    async def post(self, path: str, json=None):
        if path == "/kb/bases":
            name = (json or {}).get("name", "kb")
            kb_id = f"{name}-1"
            self.state["bases"] = self.state["bases"] + [
                {"id": kb_id, "name": name, "description": (json or {}).get("description", "")}
            ]
            return _FakeResp({"id": kb_id})
        if path.endswith("/search"):
            kb = path.split("/")[-2]
            hits = self.state["hits"].get(kb, [])
            return _FakeResp(hits)
        return _FakeResp({}, 404)


class _FakeKB:
    """离线替身 — 覆盖路由用到的 KBClient 面 (client.get/post + create_kb 等)。"""

    state = {"bases": [], "docs": {}, "hits": {}, "uploads": []}

    def __init__(self, base_url: str = "", timeout: float = 30.0):
        self.client = _FakeHttp(self.state)

    async def close(self):
        pass

    async def create_kb(self, name: str, description: str = "") -> str:
        kb_id = f"{name}-1"
        self.state["bases"] = self.state["bases"] + [
            {"id": kb_id, "name": name, "description": description}
        ]
        return kb_id

    async def search(self, kb_id: str, query: str, top_k: int = 5):
        return self.state["hits"].get(kb_id, [])

    async def upload_file(self, kb_id: str, file_path: str, file_name: str | None = None):
        self.state["uploads"].append((kb_id, file_name))
        return {"chunks": 7}

    async def list_documents(self, kb_id: str):
        return self.state["docs"].get(kb_id, [])


@pytest.fixture
def kb_state():
    _FakeKB.state = {"bases": [], "docs": {}, "hits": {}, "uploads": []}
    return _FakeKB.state


@pytest.fixture
def client(monkeypatch, tmp_path, kb_state):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr("fusion_cowork.ai.mlx_client.KBClient", _FakeKB)
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
        skills_dir=tmp_path / "skills",
        rag_base_url="http://test:11436",
        rag_wired_path=tmp_path / "wired.json",
    )
    return TestClient(app)


def _auth(key: str = STATIC_KEY):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def skill_registry_state():
    from fusion_cowork.skills.registry import SkillRegistry

    saved = dict(SkillRegistry._skills)
    yield SkillRegistry._skills
    SkillRegistry._skills.clear()
    SkillRegistry._skills.update(saved)


class TestSkills:
    def test_list_empty(self, client):
        r = client.get("/api/extensions/skills", headers=_auth())
        assert r.status_code == 200
        assert r.json() == {"skills": [], "count": 0}

    def test_install_node_pack_registers_capability(self, client, tmp_path, skill_registry_state):
        r = client.post("/api/extensions/skills", json=NODE_PACK, headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["installed"] is True
        assert body["capability"] == "skill.echo-pack"
        assert (tmp_path / "skills" / "echo-pack" / "skill.json").exists()
        rows = client.get("/api/extensions/skills", headers=_auth()).json()["skills"]
        assert rows[0]["cap_status"] == "active"

    def test_install_rejects_invalid_docs(self, client):
        for doc, fragment in [
            ({"name": "", "type": "node", "node": "x"}, "name"),
            ({"name": "bad/path", "type": "node", "node": "x"}, "name"),
            ({"name": "t1", "type": "magic"}, "type must be one of"),
            ({"name": "t2", "type": "workflow"}, "template_id"),
            ({"name": "t3", "type": "node"}, "node field"),
        ]:
            r = client.post("/api/extensions/skills", json=doc, headers=_auth())
            assert r.status_code == 400
            assert fragment in " ".join(r.json()["problems"])

    def test_script_pack_allowed_with_static_key(self, client, tmp_path, skill_registry_state):
        pdir = tmp_path / "skills" / "scr"
        pdir.mkdir(parents=True)
        (pdir / "handler.py").write_text("async def handle(args):\n    return {'echo': args}\n", encoding="utf-8")
        r = client.post(
            "/api/extensions/skills",
            json={"name": "scr", "type": "script", "script": "handler.py"},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["installed"] is True

    def test_script_pack_refused_for_module_key(self, client, monkeypatch):
        from fusion_cowork.server import web_api

        monkeypatch.setattr(web_api, "_module_key", lambda: "fm-issued-key")
        r = client.post(
            "/api/extensions/skills",
            json={"name": "scr2", "type": "script", "script": "handler.py"},
            headers=_auth("fm-issued-key"),
        )
        assert r.status_code == 403
        assert "admin-only" in " ".join(r.json()["problems"])

    def test_script_pack_requires_script_field(self, client):
        r = client.post("/api/extensions/skills", json={"name": "scr3", "type": "script"}, headers=_auth())
        assert r.status_code == 400
        assert "script field" in " ".join(r.json()["problems"])

    def test_test_run_executes_handler(self, client, tmp_path, skill_registry_state):
        pdir = tmp_path / "skills" / "scr"
        pdir.mkdir(parents=True)
        (pdir / "handler.py").write_text("async def handle(args):\n    return {'echo': args}\n", encoding="utf-8")
        client.post(
            "/api/extensions/skills",
            json={"name": "scr", "type": "script", "script": "handler.py"},
            headers=_auth(),
        )
        r = client.post("/api/extensions/skills/scr/test", json={"args": "hi"}, headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["result"] == {"echo": "hi"}
        assert body["duration_ms"] >= 0

    def test_test_run_honest_failure_envelope(self, client, tmp_path, skill_registry_state):
        pdir = tmp_path / "skills" / "broken"
        pdir.mkdir(parents=True)
        (pdir / "handler.py").write_text("async def handle(args):\n    return {'error': 'boom'}\n", encoding="utf-8")
        client.post(
            "/api/extensions/skills",
            json={"name": "broken", "type": "script", "script": "handler.py"},
            headers=_auth(),
        )
        r = client.post("/api/extensions/skills/broken/test", json={}, headers=_auth())
        body = r.json()
        assert body["ok"] is False
        assert body["result"] == {"error": "boom"}

    def test_test_run_unknown_pack_404(self, client):
        r = client.post("/api/extensions/skills/nope/test", json={}, headers=_auth())
        assert r.status_code == 404

    def test_delete_removes_pack_and_capability(self, client, tmp_path, skill_registry_state):
        client.post("/api/extensions/skills", json=NODE_PACK, headers=_auth())
        r = client.delete("/api/extensions/skills/echo-pack", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True
        assert body["capability_removed"] is True
        rows = client.get("/api/extensions/skills", headers=_auth()).json()["skills"]
        assert rows == []


class TestRagHealth:
    def test_health_ok(self, client):
        r = client.get("/api/extensions/rag/health", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == "0.8.4"


class TestRagKb:
    def test_list_filter_and_wire_state(self, client, tmp_path):
        client.post("/api/extensions/rag/kb", json={"name": "products", "description": "product docs", "wire": True}, headers=_auth())
        client.post("/api/extensions/rag/kb", json={"name": "copy", "description": "historical copy", "wire": False}, headers=_auth())
        data = client.get("/api/extensions/rag/kb", headers=_auth()).json()
        assert data["total"] == 2
        by_id = {b["kb_id"]: b for b in data["bases"]}
        assert by_id["products-1"]["cap_status"] == "active"
        assert by_id["products-1"]["capability"] == "knowledge.products-1.search"
        assert by_id["copy-1"]["capability"] is None
        filtered = client.get("/api/extensions/rag/kb", params={"q": "prod"}, headers=_auth()).json()
        assert filtered["total"] == 1
        assert (tmp_path / "wired.json").exists()

    def test_list_pagination(self, client):
        for i in range(3):
            client.post("/api/extensions/rag/kb", json={"name": f"kb{i}", "description": "", "wire": False}, headers=_auth())
        page1 = client.get("/api/extensions/rag/kb", params={"page": 1, "page_size": 2}, headers=_auth()).json()
        assert page1["total"] == 3
        assert len(page1["bases"]) == 2
        page2 = client.get("/api/extensions/rag/kb", params={"page": 2, "page_size": 2}, headers=_auth()).json()
        assert len(page2["bases"]) == 1

    def test_create_requires_name(self, client):
        r = client.post("/api/extensions/rag/kb", json={"name": " "}, headers=_auth())
        assert r.status_code == 400

    def test_wire_unwire_updates_state(self, client):
        client.post("/api/extensions/rag/kb", json={"name": "docs", "description": "", "wire": False}, headers=_auth())
        r = client.post("/api/extensions/rag/kb/docs-1/wire", json={"wired": True}, headers=_auth())
        body = r.json()
        assert body["wired"] is True
        assert body["capability"] == "knowledge.docs-1.search"
        r2 = client.post("/api/extensions/rag/kb/docs-1/wire", json={"wired": False}, headers=_auth())
        body2 = r2.json()
        assert body2["wired"] is False
        assert body2["capabilities_disabled"] == ["knowledge.docs-1.search"]

    def test_documents_list(self, client):
        _FakeKB.state["docs"]["docs-1"] = [{"file_name": "a.md", "chunk_count": 3, "char_count": 100}]
        client.post("/api/extensions/rag/kb", json={"name": "docs", "description": "", "wire": False}, headers=_auth())
        r = client.get("/api/extensions/rag/kb/docs-1/documents", headers=_auth())
        assert r.status_code == 200
        assert r.json()["documents"][0]["file_name"] == "a.md"

    def test_search_projects_hits(self, client):
        _FakeKB.state["hits"]["docs-1"] = [
            {"text": "hit text", "doc_path": "a.md", "score": 0.9, "embedding": [0.1] * 8}
        ]
        client.post("/api/extensions/rag/kb", json={"name": "docs", "description": "", "wire": False}, headers=_auth())
        r = client.post("/api/extensions/rag/kb/docs-1/search", json={"query": "what?", "top_k": 5}, headers=_auth())
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) == 1
        assert hits[0] == {"text": "hit text", "doc_path": "a.md", "score": 0.9}

    def test_search_requires_query(self, client):
        r = client.post("/api/extensions/rag/kb/x/search", json={"query": "  "}, headers=_auth())
        assert r.status_code == 400


class TestRagUpload:
    def test_upload_ingests_md(self, client, tmp_path, kb_state):
        client.post("/api/extensions/rag/kb", json={"name": "docs", "description": "", "wire": False}, headers=_auth())
        r = client.post(
            "/api/extensions/rag/kb/docs-1/documents",
            files={"file": ("note.md", b"# hello", "text/markdown")},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["chunks"] == 7
        assert kb_state["uploads"][0][1] == "note.md"

    def test_upload_rejects_non_md_txt(self, client):
        client.post("/api/extensions/rag/kb", json={"name": "docs", "description": "", "wire": False}, headers=_auth())
        r = client.post(
            "/api/extensions/rag/kb/docs-1/documents",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            headers=_auth(),
        )
        assert r.status_code == 400

    def test_upload_requires_file(self, client):
        r = client.post("/api/extensions/rag/kb/x/documents", headers=_auth())
        assert r.status_code == 400


def test_manifest_declares_extensions_page():
    manifest = json.loads((Path(__file__).resolve().parent.parent / "web" / "manifest.json").read_text(encoding="utf-8"))
    routes = [p["route"] for p in manifest.get("pages", [])]
    assert "/extensions" in routes
