"""issue #427: 扩展运行可追溯 — extension 标签 + Extension Log 交付契约 + 模板接线。

- historical_match extend 判定下发 extension_label + extension_contract; new/base 空串
- github_api 空串标签过滤 (模板按判定插值标签, 非扩展运行标签集不变)
"""

from __future__ import annotations

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode
from fusion_cowork.nodes.ops.ops_nodes import GitHubNode
from fusion_cowork.templates.template_manager import TemplateManager

import_all_nodes()


def _match(**params):
    node = HistoricalMatchNode()
    node.config.params = params
    return node


class TestMatchExtensionOutputs:
    async def test_extend_emits_label_and_contract(self):
        res = await _match(
            requirement="pet hotel booking with payments and notifications",
            candidates=[
                {
                    "slug": "pet-hotel",
                    "repo": "org/pet-hotel",
                    "capabilities": ["pet hotel", "booking", "payments"],
                    "customer": "",
                }
            ],
            own_customer="",
        ).execute({})
        assert res.data["verdict"] == "extend"
        assert res.data["extension_label"] == "extension"
        assert "Extension contract" in res.data["extension_contract"]
        assert "Extension Log" in res.data["extension_contract"]

    async def test_new_emits_empty(self):
        res = await _match(requirement="build a quantum plasma thruster console").execute({})
        assert res.data["verdict"] == "new"
        assert res.data["extension_label"] == ""
        assert res.data["extension_contract"] == ""

    async def test_base_emits_empty(self):
        res = await _match(
            requirement="pet hotel booking with payments",
            candidates=[
                {
                    "slug": "pet-hotel",
                    "repo": "org/pet-hotel",
                    "capabilities": ["pet hotel", "booking", "payments"],
                    "customer": "other-corp",
                }
            ],
            own_customer="",
        ).execute({})
        assert res.data["verdict"] == "base"
        assert res.data["extension_label"] == ""
        assert res.data["extension_contract"] == ""


class _CaptureClient:
    def __init__(self):
        self.bodies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, path, json=None, params=None, **kw):
        self.bodies.append((method, path, kw.get("json", json)))

        class R:
            status_code = 201

            def raise_for_status(self):
                return None

            def json(self):
                return {"number": 1}

        return R()


class TestGithubApiLabelFilter:
    async def test_blank_labels_dropped(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        node = GitHubNode()
        node.config.params = {
            "op": "issue_create",
            "repo": "org/ok",
            "allowed_repos": ["org/ok"],
            "title": "t",
            "labels": ["state:ready", "", "extension"],
        }
        client = _CaptureClient()
        node._client = lambda *a, **k: client
        res = await node.execute({})
        assert res.status.value == "success"
        # 首个请求是去重 search (GET), POST 建单才是标签载体
        post_body = next(json for _, _, json in client.bodies if json is not None and "labels" in (json or {}))
        assert post_body["labels"] == ["state:ready", "extension"]


class TestTemplateWiring:
    def test_n6_labels_carry_extension_placeholder(self):
        tpl = next(t for t in TemplateManager().list_templates() if t["id"] == "product_lifecycle")
        n6 = next(n for n in tpl["workflow"]["nodes"] if n["id"] == "n6")
        assert "${step.n4m.extension_label}" in n6["config"]["params"]["labels"]

    def test_n7_prompt_carries_extension_contract(self):
        tpl = next(t for t in TemplateManager().list_templates() if t["id"] == "product_lifecycle")
        n7 = next(n for n in tpl["workflow"]["nodes"] if n["id"] == "n7")
        assert "${step.n4m.extension_contract}" in n7["config"]["params"]["prompt"]

    def test_assembly_defaults_keep_labels_clean(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", {"slug": "demo", "repo": "o/r", "task_title": "t"}
        )
        labels = wf.nodes["n6"].config.params["labels"]
        # 装配期标签含插值占位符, 运行期非扩展运行为 ["state:ready", ""]
        assert "state:ready" in labels
