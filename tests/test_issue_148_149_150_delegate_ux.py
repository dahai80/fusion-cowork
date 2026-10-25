"""issue #148 + #149 + #150: chat-first 委托 / 产物就地 AI 修订 / 引用溯源。

Contract:
- #148 POST /api/ai/chat-delegate: ≤2 节点且无高风险 → mode=auto 直接执行
  (run_id 返回, 工作流落库); 3 节点或含 HIGH_RISK_NODES → mode=plan 不启动运行;
  缺 prompt 400; 生成失败 502。
- #149 产物就地修订: /ai_revise 选区不在内容中 400; mlx 桩改写 → diff_blocks
  非空; /apply_revision 写回 (block 级部分接受经 apply_block_selection 重建),
  revision_history 追加, 版本 +1; 乐观锁冲突 409。
- #150 引用溯源: AISummarize/AIClassify 输出 citations (源文件); 产物收集透传
  citations; GET /api/files/read 沙箱内读取 (行范围), 缺 path 400, 不存在 404。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

STATIC_KEY = "fc-web-test-148"


@pytest.fixture
def client(tmp_path, monkeypatch):

    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
    from fusion_cowork.server import web_api

    saved = dict(NodeRegistry._registry)
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)

    @register_node
    class MockEchoNode(BaseNode):
        name = "mock_echo_148"
        display_name = "回显"
        category = NodeCategory.TOOL

        async def execute(self, params):
            return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="echo ok")

    web_api._WEB_DIR = tmp_path / "web"
    workflow_stub = {
        "name": "委托计划",
        "description": "desc",
        "nodes": [{"id": "n1", "name": "mock_echo_148", "config": {"label": "回显"}}],
        "edges": [],
    }

    class _NL:
        plan_nodes = workflow_stub["nodes"]

        async def generate(self, prompt, context=None):
            return {**workflow_stub, "nodes": self.plan_nodes}

    app = web_api.create_web_app(
        engine=WorkflowEngine(),
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        nl_generator=_NL(),
    )
    yield TestClient(app)
    NodeRegistry._registry.clear()
    NodeRegistry._registry.update(saved)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}", "x-api-key": STATIC_KEY}


class TestIssue148ChatDelegate:
    def test_simple_plan_auto_runs(self, client):
        resp = client.post("/api/ai/chat-delegate", headers=_auth(), json={"prompt": "做个回显"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "auto"
        assert body["run_id"]
        assert body["plan"]["steps"][0]["display"] == "回显"
        run = client.get(f"/api/runs/{body['run_id']}", headers=_auth()).json()
        assert run["status"] in ("running", "completed", "success")
        wf = client.get(f"/api/workflows/{body['workflow_id']}", headers=_auth())
        assert wf.status_code == 200, "计划应落库 (画布可选编辑)"

    def test_high_risk_node_goes_plan(self, tmp_path, monkeypatch):
        # 单节点但属 HIGH_RISK_NODES → 必须走 plan 批准路径 (确定性分流, 不走模型)
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        web_api._WEB_DIR = tmp_path / "web"
        # issue #168: 本机 ~/.fusion-cowork/web_module_key 会激活鉴权 — 显式设 env key 匹配 _auth()
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)

        class _NLRisk:
            async def generate(self, prompt, context=None):
                return {"name": "高危计划", "nodes": [{"id": "n1", "name": "clipboard"}], "edges": []}

        app = web_api.create_web_app(
            engine=WorkflowEngine(), dist_dir=tmp_path / "no-dist", store_root=tmp_path / "wfr", nl_generator=_NLRisk()
        )
        c = TestClient(app)
        resp = c.post("/api/ai/chat-delegate", headers=_auth(), json={"prompt": "读剪贴板"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "plan", "高风险节点必须批准后执行"
        assert not body.get("run_id")

    def test_three_node_plan_not_auto(self, client, tmp_path, monkeypatch):
        # 直接构造 3 节点 stub 应用验证 plan 分流
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        web_api._WEB_DIR = tmp_path / "web"

        class _NL3:
            async def generate(self, prompt, context=None):
                return {
                    "name": "多步计划",
                    "nodes": [
                        {"id": "n1", "name": "mock_echo_148"},
                        {"id": "n2", "name": "mock_echo_148"},
                        {"id": "n3", "name": "mock_echo_148"},
                    ],
                    "edges": [
                        {"source_id": "n1", "target_id": "n2"},
                        {"source_id": "n2", "target_id": "n3"},
                    ],
                }

        app = web_api.create_web_app(
            engine=WorkflowEngine(), dist_dir=tmp_path / "no-dist", store_root=tmp_path / "wf3", nl_generator=_NL3()
        )
        c = TestClient(app)
        resp = c.post("/api/ai/chat-delegate", headers=_auth(), json={"prompt": "三步任务"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "plan", "3 节点不应自动执行"
        assert not body.get("run_id")
        assert len(body["plan"]["steps"]) == 3

    def test_missing_prompt_400(self, client):
        assert client.post("/api/ai/chat-delegate", headers=_auth(), json={}).status_code == 400

    def test_generate_failure_502(self, client, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        web_api._WEB_DIR = tmp_path / "web"

        class _NLBad:
            async def generate(self, prompt, context=None):
                raise RuntimeError("mlx down")

        app = web_api.create_web_app(
            engine=WorkflowEngine(), dist_dir=tmp_path / "no-dist", store_root=tmp_path / "wf4", nl_generator=_NLBad()
        )
        c = TestClient(app)
        assert c.post("/api/ai/chat-delegate", headers=_auth(), json={"prompt": "x"}).status_code == 502


class _StubMlx:
    def __init__(self, content: str = "修订后的文本"):
        self.content = content

    async def chat(self, model="", messages=None, **kw):
        return SimpleNamespace(content=self.content, usage={})

    async def list_models(self):
        return [{"id": "stub-chat", "engine_type": "text_generation"}]


@pytest.fixture
def space_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.ai.mlx_client import KBClient
    from fusion_cowork.server import web_api
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

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    store = SpaceStore(data_dir=str(tmp_path / "spaces"))
    perm = SpacePermission(store)
    chat_svc = SpaceChatService(store, _StubMlx(content="第二段已改写"), perm)
    artifact_svc = SpaceArtifactService(store, perm)
    space_api = create_space_api(
        SpaceService(store),
        SpaceMemberService(store, perm),
        chat_svc,
        SpaceKBService(store, KBClient(), perm),
        artifact_svc=artifact_svc,
        notification_svc=NotificationService(store),
        space_store=store,
    )
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_api=space_api,
    )
    yield TestClient(app), space_api, artifact_svc, store
    import asyncio

    asyncio.run(store.close())


class TestIssue149ArtifactRevise:
    def test_revise_and_apply(self, space_client):
        client, space_api, artifact_svc, store = space_client
        sid = client.post("/api/space/spaces", headers=_auth(), json={"name": "rev-space"}).json()["id"]
        art = client.post(
            f"/api/space/spaces/{sid}/artifacts",
            headers=_auth(),
            json={"name": "doc", "content": "第一段\n第二段原文\n第三段"},
        ).json()
        aid = art["id"]
        resp = client.post(
            f"/api/space/spaces/{sid}/artifacts/{aid}/ai_revise",
            headers=_auth(),
            json={"selection": "第二段原文", "instruction": "更简洁"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["revised"] == "第二段已改写"
        assert body["diff_blocks"], "选区被改写应产出 diff blocks"
        assert body["model"] == "stub-chat", "未指定 model 时应自动选 chat 模型"

    def test_apply_revision_and_history(self, space_client):
        client, space_api, artifact_svc, store = space_client
        sid = client.post("/api/space/spaces", headers=_auth(), json={"name": "apply-space"}).json()["id"]
        art = client.post(
            f"/api/space/spaces/{sid}/artifacts",
            headers=_auth(),
            json={"name": "doc2", "content": "AAA\nBBB\nCCC"},
        ).json()
        aid = art["id"]
        resp = client.post(
            f"/api/space/spaces/{sid}/artifacts/{aid}/apply_revision",
            headers=_auth(),
            json={"selection": "BBB", "revised": "B2", "model": "stub"},
        )
        assert resp.status_code == 200, resp.text
        got = client.get(f"/api/space/spaces/{sid}/artifacts/{aid}", headers=_auth()).json()
        assert "B2" in got["content"] and "BBB" not in got["content"]
        meta = got.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta and meta["revision_history"][0]["selection"] == "BBB"

    def test_apply_partial_blocks(self, space_client):
        client, space_api, artifact_svc, store = space_client
        sid = client.post("/api/space/spaces", headers=_auth(), json={"name": "blk-space"}).json()["id"]
        art = client.post(
            f"/api/space/spaces/{sid}/artifacts",
            headers=_auth(),
            json={"name": "doc3", "content": "keep old line\n目标行\nkeep too"},
        ).json()
        aid = art["id"]
        resp = client.post(
            f"/api/space/spaces/{sid}/artifacts/{aid}/apply_revision",
            headers=_auth(),
            json={
                "selection": "目标行",
                "revised": "目标行A\n目标行B",
                "accepted_blocks": [0],
            },
        )
        assert resp.status_code == 200
        got = client.get(f"/api/space/spaces/{sid}/artifacts/{aid}", headers=_auth()).json()
        assert "目标行A" in got["content"]

    def test_apply_selection_mismatch_400(self, space_client):
        client, space_api, artifact_svc, store = space_client
        sid = client.post("/api/space/spaces", headers=_auth(), json={"name": "mis-space"}).json()["id"]
        art = client.post(
            f"/api/space/spaces/{sid}/artifacts", headers=_auth(), json={"name": "d", "content": "hello"}
        ).json()
        resp = client.post(
            f"/api/space/spaces/{sid}/artifacts/{art['id']}/apply_revision",
            headers=_auth(),
            json={"selection": "不存在的选区", "revised": "x"},
        )
        assert resp.status_code == 400

    def test_ai_revise_selection_mismatch_400(self, space_client):
        client, space_api, artifact_svc, store = space_client
        sid = client.post("/api/space/spaces", headers=_auth(), json={"name": "mis2-space"}).json()["id"]
        art = client.post(
            f"/api/space/spaces/{sid}/artifacts", headers=_auth(), json={"name": "d", "content": "hello"}
        ).json()
        resp = client.post(
            f"/api/space/spaces/{sid}/artifacts/{art['id']}/ai_revise",
            headers=_auth(),
            json={"selection": "不存在的选区", "instruction": ""},
        )
        assert resp.status_code == 400


class TestIssue150Citations:
    @pytest.mark.asyncio
    async def test_summarize_emits_citations(self, tmp_path, monkeypatch):
        f1 = tmp_path / "a.md"
        f1.write_text("hello world", encoding="utf-8")
        from fusion_cowork.engine.node import NodeConfig, NodeRegistry
        from fusion_cowork.nodes.ai.classify import AISummarizeNode

        saved = dict(NodeRegistry._registry)
        try:
            NodeRegistry._registry.setdefault(AISummarizeNode.name, AISummarizeNode)
            node = AISummarizeNode(config=NodeConfig(params={"model": "stub"}))
            node._mlx = _StubMlx(content='{"title": "t", "summary": "s", "keywords": [], "conclusions": []}')
            result = await node.execute({"files": [str(f1)]})
            assert result.status.value == "success"
            citations = result.data["citations"]
            assert citations and citations[0]["path"] == str(f1) and citations[0]["type"] == "file"
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)

    def test_collect_artifacts_carries_citations(self):
        from types import SimpleNamespace

        from fusion_cowork.server.web_api import _collect_artifacts

        step = SimpleNamespace(
            node_id="n1",
            node_name="ai_summarize",
            node_display_name="AI摘要",
            output_data={
                "report": "# 报告\n内容",
                "citations": [{"type": "file", "path": "/tmp/a.md", "name": "a.md"}],
            },
        )
        arts = _collect_artifacts([step])
        assert arts, "报告产物应被收集"
        assert arts[0]["citations"][0]["path"] == "/tmp/a.md"

    def test_files_read_endpoint(self, client, tmp_path):
        f = tmp_path / "src.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
        resp = client.get("/api/files/read", headers=_auth(), params={"path": str(f), "line_start": 2, "line_end": 4})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["line_start"] == 2 and body["line_end"] == 4 and body["total_lines"] == 20
        assert body["content"].splitlines() == ["line2", "line3", "line4"]

    def test_files_read_missing_404_and_400(self, client):
        assert client.get("/api/files/read", headers=_auth(), params={"path": "/no/such/file.txt"}).status_code == 404
        assert client.get("/api/files/read", headers=_auth()).status_code == 400
