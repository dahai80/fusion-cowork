"""Issue #138 回归测试 — 审批卡持久化/scope、审批上下文、waterfall input_data、空间 instructions。

- 规则持久化: save→load 往返, 重启不丢
- confirm scope: 仅本次允许 (精确 scope) vs 始终允许 (*)
- list_pending 附带 params/ts/workflow/node_id
- _step_dict 暴露 input_data (waterfall 抽屉数据源)
- 空间 instructions: API 往返 + chat 注入
"""

from __future__ import annotations

import time

import pytest

STATIC_KEY = "test-key-138"


def test_approval_rules_survive_restart(tmp_path):
    from fusion_cowork.engine.permission_manager import PermissionManager

    target = str(tmp_path / "perms.json")
    pm1 = PermissionManager()
    pm1.approve("shell_exec")
    pm1.deny("mouse_click")
    pm1.save(target)
    pm2 = PermissionManager()
    pm2.load(target)
    assert any(r.tool_name == "shell_exec" and r.allowed for r in pm2.rules)
    assert any(r.tool_name == "mouse_click" and not r.allowed for r in pm2.rules)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    import fusion_cowork.engine.permission_manager as _perm

    monkeypatch.setattr(_perm, "_PERMISSIONS_FILE", str(tmp_path / "perms.json"))
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestApprovalScope:
    def test_allow_once_precise_scope(self, client):
        pm = client.app.state.permission_manager
        pm._pending_local_approvals["perm_scope1"] = "shell_exec"
        pm._pending_local_details["perm_scope1"] = {
            "tool_name": "shell_exec",
            "params": {"command": "git status"},
            "ts": time.time(),
        }
        try:
            r = client.post(
                "/api/permissions/pending/perm_scope1/confirm",
                json={"approved": True, "scope": "command:git"},
                headers=_auth(),
            )
            assert r.status_code == 200
            assert r.json()["confirmed"] is True
        finally:
            pm._pending_local_approvals.pop("perm_scope1", None)
            pm._pending_local_details.pop("perm_scope1", None)
        rules = [x for x in pm.rules if x.tool_name == "shell_exec"]
        assert any(x.scope == "command:git" and x.allowed for x in rules)

    def test_allow_always_scope_star(self, client):
        pm = client.app.state.permission_manager
        pm._pending_local_approvals["perm_scope2"] = "shell_exec"
        try:
            r = client.post(
                "/api/permissions/pending/perm_scope2/confirm",
                json={"approved": True},
                headers=_auth(),
            )
            assert r.json()["confirmed"] is True
        finally:
            pm._pending_local_approvals.pop("perm_scope2", None)
        assert any(x.tool_name == "shell_exec" and x.scope == "*" for x in pm.rules)


class TestApprovalContext:
    def test_list_pending_local_details(self, client):
        pm = client.app.state.permission_manager
        pm._pending_local_approvals["perm_ctx1"] = "shell_exec"
        pm._pending_local_details["perm_ctx1"] = {
            "tool_name": "shell_exec",
            "params": {"command": "ls"},
            "ts": 1729000000.0,
            "workflow": "整理下载",
            "node_id": "n1",
        }
        try:
            r = client.get("/api/permissions/pending", headers=_auth())
            entries = r.json()["approvals"]
        finally:
            pm._pending_local_approvals.pop("perm_ctx1", None)
            pm._pending_local_details.pop("perm_ctx1", None)
        hit = next(e for e in entries if e["action_id"] == "perm_ctx1")
        assert hit["source"] == "local"
        assert hit["workflow"] == "整理下载"
        assert hit["node_id"] == "n1"
        assert hit["params"]["command"] == "ls"


class TestEngineContextAndWaterfall:
    def test_denied_step_input_data_and_context(self, client):
        pm = client.app.state.permission_manager
        r = client.post(
            "/api/runs",
            json={
                "name": "wf-deny",
                "workflow": {
                    "name": "wf-deny",
                    "nodes": {"n1": {"name": "shell_exec", "config": {"label": "危险节点"}}},
                    "edges": [],
                },
            },
            headers=_auth(),
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            rec = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
            if rec.get("status") in ("completed", "failed", "canceled"):
                break
            time.sleep(0.1)
        steps = rec.get("steps") or []
        assert steps, "DENIED 步骤应记录"
        assert steps[0]["status"] == "denied"
        assert "input_data" in steps[0]
        pend = [e for e in pm.list_pending() if e.get("workflow") == "wf-deny"]
        assert pend, "拒绝应落待审批队列并带 workflow 上下文"
        # from_dict (dict 形式) 以生成 node.id 为 key — 断言上下文 node_id 与 step 一致
        assert pend[0].get("node_id") == steps[0]["node_id"]

    def test_step_dict_input_data_unit(self):
        from types import SimpleNamespace

        from fusion_cowork.server.web_api import RunRegistry

        step = SimpleNamespace(
            node_id="n1",
            node_name="shell_exec",
            node_display_name="Shell",
            status=None,
            execution_time=0.5,
            summary="",
            error=None,
            output_data={},
            input_data={"command": "ls"},
        )
        d = RunRegistry._step_dict(step)
        assert d["input_data"] == {"command": "ls"}


class TestSpaceInstructions:
    def test_space_config_roundtrip(self):
        from fusion_cowork.space.models import SpaceConfig

        c = SpaceConfig.from_dict({"instructions": "用中文回答"})
        assert c.instructions == "用中文回答"
        assert c.to_dict()["instructions"] == "用中文回答"
        assert SpaceConfig().instructions == ""

    def test_instructions_api_roundtrip(self, client):
        r = client.post("/api/space/spaces", headers=_auth(), json={"name": "知识空间", "description": ""})
        sid = r.json()["id"]
        r = client.put(
            f"/api/space/spaces/{sid}/instructions",
            json={"instructions": "引用知识库时标注来源"},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["instructions"] == "引用知识库时标注来源"
        r = client.get(f"/api/space/spaces/{sid}/instructions", headers=_auth())
        assert r.json()["instructions"] == "引用知识库时标注来源"

    def test_instructions_404(self, client):
        r = client.get("/api/space/spaces/sp_nope/instructions", headers=_auth())
        assert r.status_code == 404

    def test_chat_injection_helper(self, client):
        r = client.post("/api/space/spaces", headers=_auth(), json={"name": "注入空间", "description": ""})
        sid = r.json()["id"]
        client.put(f"/api/space/spaces/{sid}/instructions", json={"instructions": "  用中文  "}, headers=_auth())
        import asyncio

        from fusion_cowork.space.chat import SpaceChatService

        svc = SpaceChatService.__new__(SpaceChatService)
        svc._store = client.app.state.space_store
        text = asyncio.run(svc._space_instructions(sid))
        assert "用中文" in text
        assert text.startswith("[空间指令]")
        empty = asyncio.run(svc._space_instructions("sp_missing"))
        assert empty == ""
