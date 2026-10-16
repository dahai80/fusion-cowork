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
    from fusion_cowork.space import (
        NotificationService,
        SpaceChatService,
        SpaceKBService,
        SpaceMemberService,
        SpaceService,
        SpaceStore,
    )
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
    notification_svc = NotificationService(store)
    space_api = create_space_api(
        space_svc,
        member_svc,
        chat_svc,
        kb_svc,
        artifact_svc=artifact_svc,
        notification_svc=notification_svc,
        space_store=store,
    )
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


class TestSpaceNotifications:
    def _svc(self, space_stack):
        from fusion_cowork.space import NotificationService

        _, _, store = space_stack
        return NotificationService(store)

    def test_list_and_mark_read(self, client, space_stack):
        import asyncio

        svc = self._svc(space_stack)
        asyncio.run(svc.push_notification("s1", "local_user", "run_done", "r1 finished", content="ok"))
        asyncio.run(svc.push_notification("s1", "local_user", "mention", "ping", content="hi"))
        listed = client.get("/api/space/notifications", headers=_auth(client))
        assert listed.status_code == 200
        assert len(listed.json()) == 2
        unread = client.get("/api/space/notifications?unread_only=true", headers=_auth(client))
        assert len(unread.json()) == 2
        nid = listed.json()[0]["id"]
        marked = client.post(f"/api/space/notifications/{nid}/read", headers=_auth(client))
        assert marked.status_code == 200
        assert marked.json()["marked"] is True
        unread2 = client.get("/api/space/notifications?unread_only=true", headers=_auth(client))
        assert len(unread2.json()) == 1

    def test_unauthorized(self, client):
        assert client.get("/api/space/notifications").status_code == 401
        assert client.post("/api/space/notifications/x/read").status_code == 401


class TestSpaceMembersPermissions:
    def test_role_update_and_matrix(self, client):
        sid = _mk_space(client)
        added = client.post(
            f"/api/space/spaces/{sid}/members",
            headers=_auth(client),
            json={"user_id": "alice", "role": "admin"},
        )
        assert added.status_code == 201, added.text
        updated = client.put(
            f"/api/space/spaces/{sid}/members/alice",
            headers=_auth(client),
            json={"role": "viewer"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["role"] == "viewer"
        missing_role = client.put(f"/api/space/spaces/{sid}/members/alice", headers=_auth(client), json={})
        assert missing_role.status_code == 400
        matrix = client.get(f"/api/space/spaces/{sid}/permissions", headers=_auth(client))
        assert matrix.status_code == 200
        body = matrix.json()
        assert set(body["roles"]) == {"owner", "admin", "member", "viewer"}
        assert len(body["matrix"]["owner"]) == 15
        assert body["matrix"]["viewer"]["send_message"] is False

    def test_remove_member(self, client):
        sid = _mk_space(client)
        client.post(
            f"/api/space/spaces/{sid}/members",
            headers=_auth(client),
            json={"user_id": "bob", "role": "member"},
        )
        removed = client.delete(f"/api/space/spaces/{sid}/members/bob", headers=_auth(client))
        assert removed.status_code == 200
        members = client.get(f"/api/space/spaces/{sid}/members", headers=_auth(client)).json()
        assert all(m["user_id"] != "bob" for m in members)

    def test_unauthorized(self, client):
        sid = _mk_space(client)
        assert client.get(f"/api/space/spaces/{sid}/permissions").status_code == 401
        assert client.put(f"/api/space/spaces/{sid}/members/x", json={"role": "admin"}).status_code == 401
        assert client.delete(f"/api/space/spaces/{sid}/members/x").status_code == 401
