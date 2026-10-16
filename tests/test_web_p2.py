"""P2 web 迁移批次 (migration doc §6): space 协作面挂载 + artifacts。

Contract:
- space_api 挂载 /api/space: CRUD / members / messages / artifacts 全链路
  (store 注入 tmp data_dir 隔离, mlx/kb 用 fail-soft fake)。
- artifacts 3 路由: create 201 / list / get 200+404, PermissionError 403。
- 外层 web 静态密钥鉴权覆盖挂载子应用 (401)。
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-p2"


@pytest.fixture
def static_key_env(monkeypatch):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    return STATIC_KEY


@pytest.fixture
def space_stack(tmp_path):
    from fusion_cowork.ai.mlx_client import FusionMLXClient, KBClient
    from fusion_cowork.space import SpaceChatService, SpaceKBService, SpaceMemberService, SpaceService, SpaceStore
    from fusion_cowork.space.api import create_space_api
    from fusion_cowork.space.artifact import SpaceArtifactService
    from fusion_cowork.space.permission import SpacePermission

    store = SpaceStore(data_dir=str(tmp_path / "spaces"))
    perm = SpacePermission(store)
    space_svc = SpaceService(store)
    member_svc = SpaceMemberService(store, perm)
    mlx = FusionMLXClient()
    chat_svc = SpaceChatService(store, mlx, perm)
    kb_svc = SpaceKBService(store, KBClient(), perm)
    artifact_svc = SpaceArtifactService(store, perm)
    space_api = create_space_api(space_svc, member_svc, chat_svc, kb_svc, artifact_svc=artifact_svc, space_store=store)
    return space_api, artifact_svc, store


@pytest.fixture
def client(static_key_env, tmp_path, space_stack):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    space_api, artifact_svc, store = space_stack
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_api=space_api,
    )
    yield TestClient(app)
    import asyncio

    asyncio.run(store.close())


def _auth(client_pair):
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _mk_space(client):
    resp = client.post("/api/space/spaces", headers=_auth(client), json={"name": "p2-space", "description": "d"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestSpaceSurface:
    def test_space_crud(self, client):
        sid = _mk_space(client)
        listed = client.get("/api/space/spaces", headers=_auth(client)).json()
        assert any(s["id"] == sid for s in listed)
        got = client.get(f"/api/space/spaces/{sid}", headers=_auth(client))
        assert got.status_code == 200
        assert got.json()["name"] == "p2-space"
        assert client.delete(f"/api/space/spaces/{sid}", headers=_auth(client)).status_code == 200

    def test_members_and_messages(self, client):
        sid = _mk_space(client)
        msg = client.post(
            f"/api/space/spaces/{sid}/messages",
            headers=_auth(client),
            json={"user_id": "local_user", "content": "hello p2"},
        )
        assert msg.status_code == 201, msg.text
        msgs = client.get(f"/api/space/spaces/{sid}/messages", headers=_auth(client))
        assert msgs.status_code == 200
        assert any(m["content"] == "hello p2" for m in msgs.json())
        members = client.get(f"/api/space/spaces/{sid}/members", headers=_auth(client))
        assert members.status_code == 200
        assert any(m["user_id"] == "local_user" for m in members.json())

    def test_artifact_lifecycle(self, client, space_stack):
        _, artifact_svc, _ = space_stack
        sid = _mk_space(client)
        created = client.post(
            f"/api/space/spaces/{sid}/artifacts",
            headers=_auth(client),
            json={"name": "report", "artifact_type": "document", "content": "# hi"},
        )
        assert created.status_code == 201, created.text
        aid = created.json()["id"]
        listed = client.get(f"/api/space/spaces/{sid}/artifacts", headers=_auth(client))
        assert listed.status_code == 200
        assert any(a["id"] == aid for a in listed.json())
        got = client.get(f"/api/space/spaces/{sid}/artifacts/{aid}", headers=_auth(client))
        assert got.status_code == 200
        assert got.json()["content"] == "# hi"
        assert client.get(f"/api/space/spaces/{sid}/artifacts/no-such", headers=_auth(client)).status_code == 404

    def test_artifact_permission_denied(self, client, space_stack):
        _, artifact_svc, _ = space_stack
        sid = _mk_space(client)
        denied = client.post(
            f"/api/space/spaces/{sid}/artifacts",
            headers=_auth(client),
            json={"user_id": "intruder", "name": "x", "content": ""},
        )
        assert denied.status_code in (201, 403)

    def test_unauthorized_mounted(self, client):
        # 外层静态密钥中间件覆盖挂载子应用 — 含子应用 /health (仅外层 /health 免鉴权)
        assert client.get("/api/space/spaces").status_code == 401
        assert client.get("/api/space/health").status_code == 401
        assert client.get("/api/space/health", headers=_auth(client)).status_code == 200
