"""issue #387: 合成 E2E 管线 — 零真实模型 token / 零 GitHub 写 / 无人工闸门。

- github_api dry_run: 确定性伪结果 (伪 issue / 伪关联 PR), 不调 API 不需 token
- claude_dispatch dry_run: 不 spawn claude 会话, 确定性伪 PR
- studio_run model: spec.model 座位模型钉选 (合成用 replay-dev, fusion-mlx#1518)
- product_lifecycle 模板: seat_model / gates_human / node_dry_run 三参数装配
"""

from __future__ import annotations

import json

import httpx

from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode
from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode, GitHubNode
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager


def _api_node(**params):
    node = GitHubNode()
    node.config.params = {"op": "issue_create", "repo": "o/r", "allowed_repos": ["o/r"], **params}
    return node


class TestGitHubDryRun:
    async def test_issue_create_canned_no_token(self, monkeypatch):
        # dry_run 不需 GITHUB_TOKEN、不触网 — _client 被调即炸
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(GitHubNode, "_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        res = await _api_node(dry_run=True, title="task one").execute({})
        assert res.status.value == "success", res.error
        assert res.data["dry_run"] is True
        assert res.data["result"]["number"] == 1
        assert res.data["result"]["title"] == "task one"
        assert res.data["result"]["state"] == "open"

    async def test_linked_pr_canned_closing(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(GitHubNode, "_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        node = _api_node(op="linked_pr", issue_number=7, require=True, dry_run=True)
        res = await node.execute({})
        assert res.status.value == "success", res.error
        r = res.data["result"]
        assert r["found"] is True
        assert r["pr_number"] == 1
        assert r["ci_status"] == "success"
        assert r["pr_link_strength"] == "closing"

    async def test_whitelist_still_enforced(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        node = _api_node(dry_run=True)
        node.config.params["allowed_repos"] = ["o/other"]
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "白名单" in res.error

    async def test_dry_run_false_keeps_token_gate(self, monkeypatch):
        # 缺省 (false) 行为不变: 无 token 仍失败
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        res = await _api_node(title="t").execute({})
        assert res.status.value == "failed"
        assert "GITHUB_TOKEN" in res.error

    def test_schema_declares_dry_run(self):
        assert GitHubNode().get_params_schema()["properties"]["dry_run"]["default"] is False


class TestDispatchDryRun:
    def _node(self, **params):
        node = ClaudeDispatchNode()
        node.config.params = {
            "repo": "o/r",
            "allowed_repos": ["o/r"],
            "issue_number": 7,
            "prompt": "do work",
            "cwd": "/tmp",
            **params,
        }
        return node

    async def test_canned_pr_no_spawn(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            "asyncio.create_subprocess_exec",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawn")),
        )
        res = await self._node(cwd=str(tmp_path), dry_run=True).execute({})
        assert res.status.value == "success", res.error
        assert res.data["pr_url"] == "https://github.invalid/o/r/pull/1"
        assert res.data["pr_number"] == 1
        assert res.data["total_tokens"] == 0
        assert res.data["dry_run"] is True

    async def test_cwd_still_required(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        res = await self._node(cwd="/nonexistent-dir-387", dry_run=True).execute({})
        assert res.status.value == "failed"
        assert "工作目录不存在" in res.error

    async def test_whitelist_still_enforced(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        node = self._node(cwd=str(tmp_path), dry_run=True)
        node.config.params["allowed_repos"] = ["o/other"]
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "白名单" in res.error


def _studio_node(**extra):
    params = {"input": "write doc", "agent_id": "a1", "mode": "async", **extra}
    return StudioRunNode(node_id="s1", config=type("C", (), {"params": params})())


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestStudioModel:
    async def test_model_sent_as_spec(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})

        _patch_transport(monkeypatch, handler)
        res = await _studio_node(model="replay-dev").execute({})
        assert res.status.value == "success", res.error
        assert seen["body"]["spec"] == {"model": "replay-dev"}

    async def test_model_and_turns_combined(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})

        _patch_transport(monkeypatch, handler)
        res = await _studio_node(model="replay-dev", max_turns=24).execute({})
        assert res.status.value == "success", res.error
        assert seen["body"]["spec"] == {"max_turns": 24, "model": "replay-dev"}

    async def test_empty_model_omits_spec(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})

        _patch_transport(monkeypatch, handler)
        res = await _studio_node().execute({})
        assert res.status.value == "success", res.error
        assert "spec" not in seen["body"]

    def test_schema_declares_model(self):
        assert "model" in _studio_node().get_params_schema()["properties"]


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


class TestTemplateSynthetic:
    def test_new_params_defaults(self):
        props = _tpl()["params"]["properties"]
        assert props["seat_model"]["default"] == ""
        assert props["gates_human"]["default"] is True
        assert props["node_dry_run"]["default"] is False

    def test_placeholders_wired(self):
        nodes = {n["id"]: n for n in _tpl()["workflow"]["nodes"]}
        for nid in ("n1", "n3", "n9"):
            assert nodes[nid]["config"]["params"]["model"] == "${param.seat_model}", nid
        for nid in ("n2", "n10"):
            assert nodes[nid]["config"]["params"]["require_review"] == "${param.gates_human}", nid
        for nid in ("n6", "n7", "n8", "n11", "n12"):
            assert nodes[nid]["config"]["params"]["dry_run"] == "${param.node_dry_run}", nid

    def test_synthetic_assembly(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle",
            param_values={"seat_model": "replay-dev", "gates_human": False, "node_dry_run": True},
        )
        for nid in ("n1", "n3", "n9"):
            assert wf.nodes[nid].config.params["model"] == "replay-dev", nid
        for nid in ("n2", "n10"):
            assert wf.nodes[nid].config.params["require_review"] is False, nid
        for nid in ("n6", "n7", "n8", "n11", "n12"):
            assert wf.nodes[nid].config.params["dry_run"] is True, nid
        # 闸 B 仍由 review_arch 独立控制 (缺省 false = 自动通过)
        assert wf.nodes["n4"].config.params["require_review"] is False

    def test_default_assembly_production(self):
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        assert wf.nodes["n1"].config.params["model"] == ""
        assert wf.nodes["n2"].config.params["require_review"] is True
        assert wf.nodes["n10"].config.params["require_review"] is True
        assert wf.nodes["n6"].config.params["dry_run"] is False
        assert wf.nodes["n7"].config.params["dry_run"] is False
