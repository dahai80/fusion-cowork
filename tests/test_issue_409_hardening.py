"""issue #409: 管线节点加固 — studio_run 等待可配 + github_api 搜索 q 256 上限。

- studio_run timeout 缺省 900 → 1800 (并发 mlx 争抢下 900s 不够); 模板参数
  studio_wait_timeout 装配进 n1/n3/n9
- github_api issue_create 去重搜索 q > 256 会 422 — 长标题截断 (建 issue 用完整标题)
"""

from __future__ import annotations

import httpx

from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode
from fusion_cowork.nodes.ops.ops_nodes import GitHubNode
from fusion_cowork.templates.template_manager import TemplateManager


class TestStudioWait:
    def test_default_timeout_1800(self):
        schema = StudioRunNode().get_params_schema()
        assert schema["properties"]["timeout"]["default"] == 1800.0

    def test_template_wires_wait_timeout_into_seats(self):
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        for node_id in ("n1", "n3", "n9"):
            node = wf.nodes[node_id]
            assert node.config.params["timeout"] == 1800
        tpl = TemplateManager().get_template("product_lifecycle")
        props = tpl["params"]["properties"]
        assert props["studio_wait_timeout"]["default"] == 1800


def _api_node(**params):
    node = GitHubNode()
    node.config.params = {"op": "issue_create", "repo": "o/r", "allowed_repos": ["o/r"], **params}
    return node


class TestSearchQCap:
    async def test_long_title_search_q_capped_at_256(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t409")
        long_title = "Pet hotel booking platform with stores, dates, notifications, payments and admin console " * 4
        assert len(long_title) > 256
        qs = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                qs.append(str(request.url.params.get("q", "")))
                return httpx.Response(200, json={"items": []})
            if request.url.path == "/repos/o/r/issues" and request.method == "POST":
                return httpx.Response(201, json={"number": 9, "title": long_title, "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        res = await _api_node(title=long_title, body="b").execute({})
        assert res.status.value == "success", res.error
        assert len(qs) == 1
        assert len(qs[0]) <= 256
        # 建 issue 用完整标题 — 截断只作用于 best-effort 去重搜索
        assert res.data["result"]["title"] == long_title

    async def test_short_title_q_untouched(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t409")
        qs = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                qs.append(str(request.url.params.get("q", "")))
                return httpx.Response(200, json={"items": []})
            if request.url.path == "/repos/o/r/issues" and request.method == "POST":
                return httpx.Response(201, json={"number": 9, "title": "Do the thing", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        res = await _api_node(title="Do the thing", body="b").execute({})
        assert res.status.value == "success"
        assert qs[0] == 'repo:o/r is:issue "Do the thing"'
