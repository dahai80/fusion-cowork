import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fusion_cowork.capabilities import (
    CapabilityStore,
    make_capability_gate,
    record_run_usage,
)

WORKSPACE_A = "sp_cap_a"
WORKSPACE_B = "sp_cap_b"
STATIC_KEY = "test-key-228"

CONNECTOR_CFG = {
    "id": "sheets",
    "name": "Google Sheets",
    "description": "读取与写入表格",
    "transport": "mcp",
    "url": "http://mcp.local:7777",
    "scopes": [{"id": "sheets.read"}, {"id": "sheets.write", "sensitive": True}],
    "oauth": {
        "authorize_url": "https://auth.example/authorize",
        "token_url": "https://auth.example/token",
        "client_id_env": "TEST_OAUTH_CLIENT_ID",
        "client_secret_env": "TEST_OAUTH_SECRET",
        "scopes": ["sheets.read", "sheets.write"],
    },
}


def _mock_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/graphs/g_research/execute":
        return httpx.Response(200, json={"events": [], "status": "completed"})
    if path == "/v1/graphs":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "graph_id": "g_research",
                        "name": "研究员",
                        "description": "拆解调研任务",
                        "graph_data": {"nodes": [{"name": "search"}, {"name": "summarize"}]},
                    }
                ]
            },
        )
    if path == "/v1/agents":
        return httpx.Response(200, json={"data": [{"agent_id": "a_reviewer", "name": "审查员", "status": "active"}]})
    if path == "/v1/graphs/g_research":
        return httpx.Response(200, json={"graph_id": "g_research", "graph_data": {"nodes": []}})
    if path == "/v1/agents/a_reviewer/definition":
        return httpx.Response(200, json={"agent_id": "a_reviewer", "tools": ["read_file"]})
    if str(request.url).startswith("https://auth.example/token"):
        return httpx.Response(
            200,
            json={
                "access_token": "at-secret-123",
                "refresh_token": "rt-secret-456",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )
    return httpx.Response(404, json={"error": "not mocked", "path": path})


@pytest.fixture
def caps_path(tmp_path):
    return tmp_path / "capabilities.json"


@pytest.fixture
def connector_dir(tmp_path):
    root = tmp_path / "connectors"
    root.mkdir()
    (root / "sheets.json").write_text(json.dumps(CONNECTOR_CFG), encoding="utf-8")
    return root


@pytest.fixture
def skills_dir(tmp_path):
    root = tmp_path / "skills"
    pack = root / "echo-pack"
    pack.mkdir(parents=True)
    (pack / "skill.json").write_text(
        json.dumps({"name": "echo-pack", "type": "node", "node": "ai_summarize", "description": "回声技能"}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def wire(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OAUTH_CLIENT_ID", "client-abc")
    monkeypatch.setenv("TEST_OAUTH_SECRET", "secret-xyz")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("FUSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    wired = tmp_path / "rag_wired.json"
    wired.write_text(json.dumps({"wired": ["kb_docs"]}), encoding="utf-8")
    return wired


@pytest.fixture
def app(tmp_path, caps_path, connector_dir, skills_dir, wire, monkeypatch):
    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    application = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
        skills_dir=skills_dir,
        rag_wired_path=wire,
        capabilities_path=caps_path,
        connector_dir=connector_dir,
        studio_base_url="http://studio.test",
        capabilities_transport=httpx.MockTransport(_mock_handler),
    )

    async def _fake_get_space(space_id):
        return object() if space_id in (WORKSPACE_A, WORKSPACE_B) else None

    monkeypatch.setattr(application.state.space_store, "get_space", _fake_get_space)
    return application


@pytest.fixture
def client(app):
    with TestClient(app, headers={"Authorization": f"Bearer {STATIC_KEY}"}) as c:
        yield c


class TestCatalog:
    def test_catalog_merges_three_sources(self, client):
        r = client.get("/api/capabilities", params={"workspace_id": WORKSPACE_A})
        assert r.status_code == 200
        body = r.json()
        ids = {i["id"] for i in body["skills"]}
        assert "skill.echo-pack" in ids
        connector_ids = {i["id"] for i in body["connectors"]}
        assert "connector.sheets" in connector_ids
        assert "connector.knowledge.kb_docs" in connector_ids
        subagent_ids = {i["id"] for i in body["subagents"]}
        assert "subagent.graph.g_research" in subagent_ids
        assert "subagent.agent.a_reviewer" in subagent_ids
        assert any(i["id"].startswith("subagent.template.") for i in body["subagents"])
        assert body["sources"][0]["name"] == "mcp_registry"

    def test_studio_down_is_reported_honestly(self, tmp_path, caps_path, connector_dir, monkeypatch):
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)

        app2 = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "w2",
            space_data_dir=tmp_path / "sp2",
            skills_dir=tmp_path / "skills2",
            rag_wired_path=tmp_path / "none.json",
            capabilities_path=caps_path,
            connector_dir=connector_dir,
            studio_base_url="http://127.0.0.1:9",
        )
        with TestClient(app2, headers={"Authorization": f"Bearer {STATIC_KEY}"}) as c:
            body = c.get("/api/capabilities", params={"type": "subagent"}).json()
        studio = next(s for s in body["sources"] if s["name"] == "agent_studio")
        assert studio["available"] is False

    def test_search_hits_capabilities(self, client):
        body = client.get("/api/search", params={"q": "sheets"}).json()
        ids = [i.get("capability_id") for i in body["items"] if i.get("kind") == "capability"]
        assert "connector.sheets" in ids


class TestInstallScope:
    def test_install_is_workspace_scoped(self, client, app, caps_path):
        r = client.post(
            "/api/capabilities/connector/connector.sheets/install",
            json={"workspace_id": WORKSPACE_A},
        )
        assert r.status_code == 200, r.text
        assert r.json()["capability"]["installed_at"] > 0
        a = client.get("/api/capabilities", params={"workspace_id": WORKSPACE_A}).json()
        sheets_a = next(i for i in a["connectors"] if i["id"] == "connector.sheets")
        assert sheets_a["installed"] is True and sheets_a["enabled"] is True
        b = client.get("/api/capabilities", params={"workspace_id": WORKSPACE_B}).json()
        sheets_b = next(i for i in b["connectors"] if i["id"] == "connector.sheets")
        assert sheets_b["installed"] is False
        saved = json.loads(caps_path.read_text(encoding="utf-8"))
        assert "connector.sheets" in saved["workspaces"][WORKSPACE_A]["connector"]
        assert WORKSPACE_B not in saved["workspaces"] or not saved["workspaces"][WORKSPACE_B].get("connector")

    def test_install_unknown_capability_404(self, client):
        r = client.post("/api/capabilities/skill/skill.nope/install", json={"workspace_id": WORKSPACE_A})
        assert r.status_code == 404

    def test_disable_per_workspace(self, client):
        client.post("/api/capabilities/skill/skill.echo-pack/install", json={"workspace_id": WORKSPACE_A})
        r = client.patch(
            "/api/capabilities/skill/skill.echo-pack/enabled",
            json={"workspace_id": WORKSPACE_A, "enabled": False},
        )
        assert r.status_code == 200
        assert r.json()["capability"]["enabled"] is False
        body = client.get("/api/capabilities", params={"workspace_id": WORKSPACE_A}).json()
        item = next(i for i in body["skills"] if i["id"] == "skill.echo-pack")
        assert item["installed"] is True and item["enabled"] is False

    def test_uninstall(self, client):
        client.post("/api/capabilities/skill/skill.echo-pack/install", json={"workspace_id": WORKSPACE_A})
        r = client.delete("/api/capabilities/skill/skill.echo-pack", params={"workspace_id": WORKSPACE_A})
        assert r.json()["uninstalled"] is True
        assert (
            client.delete("/api/capabilities/skill/skill.echo-pack", params={"workspace_id": WORKSPACE_A}).json()[
                "uninstalled"
            ]
            is False
        )

    def test_install_rejects_unknown_workspace(self, client):
        r = client.post("/api/capabilities/skill/skill.echo-pack/install", json={"workspace_id": "sp_missing"})
        assert r.status_code == 404


class TestGate:
    async def _gate(self, store, connector_dir, wire):
        return make_capability_gate(store, connector_dir=connector_dir, rag_wired_path=wire)

    async def test_pool_empty_allows_legacy_calls(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        gate = await self._gate(store, connector_dir, wire)
        verdict, _ = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "allow"

    async def test_not_installed_denied_once_pool_active(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets", name="Sheets", source="connector_config")
        store.install(WORKSPACE_B, "connector", "connector.knowledge.kb_docs", name="kb", source="knowledge")
        gate = await self._gate(store, connector_dir, wire)
        verdict, reason = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_B)
        assert verdict == "deny"
        assert "未安装" in reason

    async def test_installed_enabled_allows_read_scope(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        store.set_credentials(WORKSPACE_A, "connector.sheets", {"access_token": "tok"})
        gate = await self._gate(store, connector_dir, wire)
        verdict, _ = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "allow"

    async def test_oauth_unconnected_requires_approval_then_allows(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        gate = await self._gate(store, connector_dir, wire)
        verdict, reason = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "approval" and "OAuth" in reason
        store.set_credentials(WORKSPACE_A, "connector.sheets", {"access_token": "tok"})
        verdict, _ = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "allow"

    async def test_disabled_denies(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        store.set_enabled(WORKSPACE_A, "connector", "connector.sheets", False)
        gate = await self._gate(store, connector_dir, wire)
        verdict, reason = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "deny" and "停用" in reason

    async def test_sensitive_scope_requires_approval_until_granted(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        gate = await self._gate(store, connector_dir, wire)
        verdict, reason = await gate(
            "mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.write"}, WORKSPACE_A
        )
        assert verdict == "approval" and "敏感 scope" in reason
        store.grant_scopes(WORKSPACE_A, "connector.sheets", ["sheets.write"])
        store.set_credentials(WORKSPACE_A, "connector.sheets", {"access_token": "tok"})
        verdict, _ = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.write"}, WORKSPACE_A)
        assert verdict == "allow"

    async def test_oauth_connector_unconnected_requires_approval(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        store.grant_scopes(WORKSPACE_A, "connector.sheets", ["sheets.write"])
        gate = await self._gate(store, connector_dir, wire)
        verdict, reason = await gate("mcp_client", {"url": "http://mcp.local:7777", "tool": "sheets.read"}, WORKSPACE_A)
        assert verdict == "approval" and "OAuth" in reason

    async def test_skill_gate_denies_uninstalled_skill(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "skill", "skill.echo-pack")
        gate = await self._gate(store, connector_dir, wire)
        verdict, _ = await gate("skill.echo-pack", {}, WORKSPACE_A)
        assert verdict == "allow"
        verdict, _ = await gate("skill.other", {}, WORKSPACE_A)
        assert verdict == "deny"


class TestScopesAndOAuth:
    def test_sensitive_scope_requires_explicit_approval(self, client, app):
        client.post("/api/capabilities/connector/connector.sheets/install", json={"workspace_id": WORKSPACE_A})
        r = client.post(
            "/api/capabilities/connectors/connector.sheets/scopes",
            json={"workspace_id": WORKSPACE_A, "scopes": ["sheets.write"]},
        )
        assert r.status_code == 409
        r = client.post(
            "/api/capabilities/connectors/connector.sheets/scopes",
            json={"workspace_id": WORKSPACE_A, "scopes": ["sheets.write"], "approve": True},
        )
        assert r.status_code == 200, r.text
        assert "sheets.write" in r.json()["capability"]["granted_scopes"]

    def test_oauth_start_builds_authorize_url(self, client, app):
        client.post("/api/capabilities/connector/connector.sheets/install", json={"workspace_id": WORKSPACE_A})
        r = client.post("/api/capabilities/connectors/connector.sheets/oauth/start", json={"workspace_id": WORKSPACE_A})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["authorize_url"].startswith("https://auth.example/authorize?")
        assert "client_id=client-abc" in body["authorize_url"]
        assert "state=" in body["authorize_url"]
        assert "code_challenge=" in body["authorize_url"]

    def test_oauth_callback_stores_encrypted_tokens_and_scopes(self, client, app, caps_path):
        client.post("/api/capabilities/connector/connector.sheets/install", json={"workspace_id": WORKSPACE_A})
        started = client.post(
            "/api/capabilities/connectors/connector.sheets/oauth/start", json={"workspace_id": WORKSPACE_A}
        ).json()
        r = client.get("/api/capabilities/oauth/callback", params={"code": "code-1", "state": started["state"]})
        assert r.status_code == 200, r.text
        assert r.json()["connected"] is True
        saved = json.loads(caps_path.read_text(encoding="utf-8"))
        creds = saved["workspaces"][WORKSPACE_A]["connector"]["connector.sheets"]["credentials"]
        assert creds["access_token"].startswith("fernet:")
        assert "at-secret-123" not in json.dumps(saved)
        entry = saved["workspaces"][WORKSPACE_A]["connector"]["connector.sheets"]
        assert "sheets.read" in entry["granted_scopes"]
        r = client.post("/api/capabilities/connectors/connector.sheets/revoke", json={"workspace_id": WORKSPACE_A})
        assert r.json()["revoked"] is True

    def test_oauth_callback_rejects_unknown_state(self, client):
        r = client.get("/api/capabilities/oauth/callback", params={"code": "x", "state": "bogus"})
        assert r.status_code == 400


class TestUsageAudit:
    def test_run_usage_recorded_per_workspace(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        store.install(WORKSPACE_A, "subagent", "subagent.graph.g_research")
        record = {
            "run_id": "run_1",
            "workflow_name": "周报",
            "space_id": WORKSPACE_A,
            "labels": {"actor": "dahai"},
            "steps": [
                {
                    "node_name": "mcp_client",
                    "input_data": {"url": "http://mcp.local:7777", "tool": "sheets.read"},
                    "status": "success",
                },
                {"node_name": "studio_run", "input_data": {"graph_id": "g_research"}, "status": "success"},
                {"node_name": "file_copy", "input_data": {}, "status": "success"},
            ],
        }
        assert record_run_usage(store, record, connector_dir=connector_dir, rag_wired_path=wire) == 2
        usage = store.usage(WORKSPACE_A, "connector", "connector.sheets")
        assert usage["usage"]["count"] == 1
        assert usage["audit"][0]["run_id"] == "run_1"
        assert usage["audit"][0]["actor"] == "dahai"
        assert store.usage(WORKSPACE_B, "connector", "connector.sheets") is None

    def test_usage_route(self, client, app):
        client.post("/api/capabilities/skill/skill.echo-pack/install", json={"workspace_id": WORKSPACE_A})
        r = client.get("/api/capabilities/skill/skill.echo-pack/usage", params={"workspace_id": WORKSPACE_A})
        assert r.status_code == 200
        assert r.json()["usage"]["count"] == 0
        r = client.get("/api/capabilities/skill/skill.echo-pack/usage", params={"workspace_id": WORKSPACE_B})
        assert r.status_code == 404

    def test_denied_step_audited_as_denied(self, caps_path, connector_dir, wire):
        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        record = {
            "run_id": "run_2",
            "space_id": WORKSPACE_A,
            "steps": [
                {
                    "node_name": "mcp_client",
                    "input_data": {"url": "http://mcp.local:7777", "tool": "sheets.read"},
                    "status": "denied",
                }
            ],
        }
        record_run_usage(store, record, connector_dir=connector_dir, rag_wired_path=wire)
        assert store.usage(WORKSPACE_A, "connector", "connector.sheets")["audit"][0]["verdict"] == "denied"


class TestSubagents:
    def test_definition_for_graph(self, client):
        r = client.get("/api/capabilities/subagents/subagent.graph.g_research/definition")
        assert r.status_code == 200, r.text
        assert r.json()["definition"]["graph_id"] == "g_research"

    def test_spawn_requires_install(self, client, app):
        r = client.post(
            "/api/capabilities/subagents/subagent.graph.g_research/spawn",
            json={"workspace_id": WORKSPACE_A, "input": "调研"},
        )
        assert r.status_code == 409

    def test_spawn_template_wires_run_registry(self, client, app, monkeypatch):
        template = next(
            i
            for i in client.get("/api/capabilities", params={"type": "subagent"}).json()["subagents"]
            if i["source"] == "dag_template"
        )
        client.post(f"/api/capabilities/subagent/{template['id']}/install", json={"workspace_id": WORKSPACE_A})
        calls = {}

        def fake_start(workflow_def, **kwargs):
            calls.update(kwargs)
            calls["nodes"] = len(workflow_def.get("nodes", []))
            return {"run_id": "run_spawn", "status": "running"}

        monkeypatch.setattr(app.state.run_registry, "start", fake_start)
        r = client.post(
            f"/api/capabilities/subagents/{template['id']}/spawn",
            json={"workspace_id": WORKSPACE_A, "input": ""},
        )
        assert r.status_code == 200, r.text
        assert r.json()["run_id"] == "run_spawn"
        assert calls["space_id"] == WORKSPACE_A
        assert calls["source"] == "capability"
        assert calls["nodes"] > 0

    def test_spawn_graph_calls_studio(self, client, app):
        client.post(
            "/api/capabilities/subagent/subagent.graph.g_research/install",
            json={"workspace_id": WORKSPACE_A},
        )
        url = "/api/capabilities/subagents/subagent.graph.g_research/spawn"
        r = client.post(url, json={"workspace_id": WORKSPACE_A, "input": "调研竞品"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "spawn_approval_required"
        r = client.post(url, json={"workspace_id": WORKSPACE_A, "input": "调研竞品", "approve": True})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "graph"


class TestPermissionIntegration:
    async def test_manager_gate_deny_blocks_and_approval_queues(self, caps_path, connector_dir, wire):
        from fusion_cowork.engine.permission_manager import (
            PermissionManager,
            reset_approval_context,
            set_approval_context,
        )

        store = CapabilityStore(caps_path)
        store.install(WORKSPACE_A, "connector", "connector.sheets")
        manager = PermissionManager()
        manager.capability_gate = make_capability_gate(store, connector_dir=connector_dir, rag_wired_path=wire)

        token = set_approval_context({"workspace_id": WORKSPACE_B})
        try:
            allowed = await manager.check("mcp_client", params={"url": "http://mcp.local:7777", "tool": "sheets.read"})
            assert allowed is False
        finally:
            reset_approval_context(token)

        token = set_approval_context({"workspace_id": WORKSPACE_A})
        try:
            allowed = await manager.check("mcp_client", params={"url": "http://mcp.local:7777", "tool": "sheets.read"})
            assert allowed is False
            assert manager.list_pending()
        finally:
            reset_approval_context(token)

    def test_app_wires_recorder_and_engine_workspace(self, app):
        assert callable(app.state.run_registry.capability_recorder)
        assert callable(app.state.run_registry._engine.set_run_workspace)
