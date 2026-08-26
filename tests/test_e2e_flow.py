"""Stage 7 E2E — tenant → JWT auth → space → message → agent → workflow → assert。

httpx ASGITransport 打 FastAPI create_space_api, JWT (HS256) 校验 + tenant 隔离。
@ pytest.mark.slow (CI 单独 job, 默认 skip 以保本地快)。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.slow]

jwt_py = pytest.importorskip("jwt")
httpx = pytest.importorskip("httpx")

from fusion_cowork.auth import jwt as jwt_mod
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.space.api import create_space_api
from fusion_cowork.space.chat import SpaceChatService
from fusion_cowork.space.knowledge import SpaceKBService
from fusion_cowork.space.member import SpaceMemberService
from fusion_cowork.space.permission import SpacePermission
from fusion_cowork.space.service import SpaceService
from fusion_cowork.space.store import SpaceStore

SECRET = "e2e-test-secret-very-long-aaaa-bbbb-cccc-32bytes"


def _token(tenant: str, user: str) -> str:
    return jwt_py.encode(
        {"tenant_id": tenant, "user_id": user, "sub": user, "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )


def _reset_verifier():
    jwt_mod._DEFAULT_VERIFIER = None


@pytest.fixture
async def store(tmp_path):
    s = SpaceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def perm(store):
    return SpacePermission(store)


@pytest.fixture
def api_app(store, perm, monkeypatch):
    _reset_verifier()
    monkeypatch.setenv("FUSION_JWT_SECRET", SECRET)
    mlx_mock = AsyncMock()
    mlx_mock.chat = AsyncMock(return_value=type("R", (), {"content": "ok", "text": "ok"})())
    kb_mock = AsyncMock()
    kb_mock.list_bases = AsyncMock(return_value=[])
    kb_mock.create_kb = AsyncMock(return_value="kb1")
    kb_mock.search = AsyncMock(return_value=[])
    kb_mock.health = AsyncMock(return_value=True)
    space_svc = SpaceService(store)
    member_svc = SpaceMemberService(store, perm)
    chat_svc = SpaceChatService(store, mlx_mock, perm)
    kb_svc = SpaceKBService(store, kb_mock, perm)
    app = create_space_api(space_svc, member_svc, chat_svc, kb_svc)
    yield app
    _reset_verifier()


@pytest.fixture
def client(api_app):
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test")


class TestE2EFlow:
    async def test_full_flow_with_jwt(self, client):
        headers_a = {"Authorization": f"Bearer {_token('e2e_tenant', 'alice')}"}
        async with client as c:
            # 1. create space
            resp = await c.post("/spaces", json={"name": "e2e-space"}, headers=headers_a)
            assert resp.status_code == 201, resp.text
            space_id = resp.json()["id"]

            # 2. list spaces — only this tenant's
            resp = await c.get("/spaces", headers=headers_a)
            assert resp.status_code == 200
            ids = [s["id"] for s in resp.json()]
            assert space_id in ids

            # 3. send message
            resp = await c.post(
                f"/spaces/{space_id}/messages",
                json={"user_id": "alice", "content": "hello e2e"},
                headers=headers_a,
            )
            assert resp.status_code in (200, 201), resp.text

            # 4. list messages
            resp = await c.get(f"/spaces/{space_id}/messages", headers=headers_a)
            assert resp.status_code == 200

    async def test_cross_tenant_isolation(self, client):
        headers_a = {"Authorization": f"Bearer {_token('t_alpha', 'alice')}"}
        headers_b = {"Authorization": f"Bearer {_token('t_beta', 'bob')}"}
        async with client as c:
            resp = await c.post("/spaces", json={"name": "alpha-space"}, headers=headers_a)
            assert resp.status_code == 201
            space_id = resp.json()["id"]

            # B cannot see A's space
            resp = await c.get(f"/spaces/{space_id}", headers=headers_b)
            assert resp.status_code == 404

            # B cannot list A's space
            resp = await c.get("/spaces", headers=headers_b)
            assert space_id not in [s["id"] for s in resp.json()]

    async def test_unauthenticated_with_jwt_active_rejected(self, client):
        async with client as c:
            # JWT active, no token -> 401
            resp = await c.post("/spaces", json={"name": "noauth"})
            assert resp.status_code == 401

    async def test_invalid_token_rejected(self, client):
        async with client as c:
            resp = await c.post(
                "/spaces",
                json={"name": "bad"},
                headers={"Authorization": "Bearer not.a.valid.jwt"},
            )
            assert resp.status_code == 401


class _E2EDemoNode(BaseNode):
    node_type = "e2e_demo"
    category = NodeCategory.LOGIC

    async def execute(self, inputs: dict):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True, "msg": "hello-e2e"})


@register_node
class _E2EDemoRegistered(_E2EDemoNode):
    node_type = "e2e_demo_reg"


class TestE2EWorkflow:
    async def test_workflow_engine_dag_executes(self, store):
        # 独立 workflow 跑通 — 验证引擎层不被多租户改造破坏
        wf = Workflow(name="e2e-wf")
        wf.add_node(_E2EDemoRegistered(node_id="n1"))
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status.value in ("completed", "success") or execution.error is None
        step = next((s for s in execution.steps if s.node_id == "n1"), None)
        assert step is not None
        out = getattr(step, "output_data", None) or getattr(step, "result", None)
        assert "hello-e2e" in str(out)
