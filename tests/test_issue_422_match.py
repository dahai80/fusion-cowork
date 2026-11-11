"""issue #422: historical_match 节点 + repo_scaffold extend 路径 + 模板接线。

- match: 确定性能力重叠打分, 三值判定 extend/base/new; base 不泄露他客户项目名
- scaffold extend: verdict=extend + extend_repo 时 clone 已有仓 (不建新脚手架),
  token 只走环境变量; 已有工作树复用
- product_lifecycle 模板: n4m (match) 接在闸 B 与脚手架之间, verdict/matched_repo
  接进 n5
"""

from __future__ import annotations

import json

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode, score_overlap
from fusion_cowork.nodes.ops.ops_nodes import RepoScaffoldNode
from fusion_cowork.templates.template_manager import TemplateManager

import_all_nodes()


def _match(**params):
    node = HistoricalMatchNode()
    node.config.params = params
    return node


class TestScoring:
    def test_overlap_basic(self):
        s = score_overlap("pet hotel booking with payments", ["pet hotel", "booking", "payments"])
        assert s > 0.5

    def test_overlap_disjoint(self):
        assert score_overlap("quantum plasma thruster", ["pet hotel", "booking"]) == 0.0

    def test_overlap_empty(self):
        assert score_overlap("", ["x"]) == 0.0
        assert score_overlap("hello", []) == 0.0


class TestMatchVerdicts:
    async def test_new_when_no_candidates(self):
        res = await _match(requirement="build a pet hotel app").execute({})
        assert res.status.value == "success"
        assert res.data["verdict"] == "new"
        assert res.data["matched_repo"] == ""

    async def test_extend_own_project(self):
        res = await _match(
            requirement="pet hotel booking with payments and notifications",
            candidates=[
                {
                    "slug": "ai-pet-hotel",
                    "repo": "o/ai-pet-hotel",
                    "capabilities": ["booking", "payments"],
                    "customer": "",
                }
            ],
            required_capabilities=["booking", "payments", "loyalty"],
        ).execute({})
        assert res.data["verdict"] == "extend"
        assert res.data["matched"]["repo"] == "o/ai-pet-hotel"
        assert res.data["gaps"] == ["loyalty"]
        assert res.data["matched_repo"] == "o/ai-pet-hotel"

    async def test_base_hides_customer_name(self):
        # issue #422 隐私红线: 命中他客户项目 → base, matched_visible 必须为空
        res = await _match(
            requirement="pet hotel booking with payments",
            candidates=[
                {
                    "slug": "ai-pet-hotel",
                    "repo": "o/ai-pet-hotel",
                    "capabilities": ["pet hotel", "booking", "payments"],
                    "customer": "tenant-a",
                }
            ],
            own_customer="tenant-b",
        ).execute({})
        assert res.data["verdict"] == "base"
        # issue #444: 原始 matched 也置空 — runs API 逐字暴露 step output_data
        assert res.data["matched"] == {}
        assert res.data["matched_visible"] == {}  # 新客户可见面零泄露
        assert res.data["matched_repo"] == ""

    async def test_below_threshold_is_new(self):
        res = await _match(
            requirement="quantum plasma thruster control",
            candidates=[{"slug": "x", "repo": "o/x", "capabilities": ["pet hotel booking"]}],
            threshold=0.6,
        ).execute({})
        assert res.data["verdict"] == "new"

    async def test_candidates_file(self, tmp_path):
        f = tmp_path / "candidates.json"
        f.write_text(json.dumps([{"slug": "ai-pet", "repo": "o/ai-pet", "capabilities": ["pet hotel booking"]}]))
        res = await _match(requirement="pet hotel booking", candidates_file=str(f), threshold=0.5).execute({})
        assert res.data["verdict"] == "extend"

    async def test_missing_requirement_fails(self):
        res = await _match().execute({})
        assert res.status.value == "failed"


class TestScaffoldExtend:
    async def test_extend_clones_existing_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t422")
        seen = {}

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env", {})

            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return P()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        node = RepoScaffoldNode()
        node.config.params = {
            "slug": "x",
            "base_dir": str(tmp_path),
            "verdict": "extend",
            "extend_repo": "o/ai-pet-hotel",
        }
        res = await node.execute({})
        assert res.status.value == "success", res.error
        assert res.data["extended"] is True
        assert res.data["repo_path"] == str(tmp_path / "ai-pet-hotel")
        assert "clone" in " ".join(seen["argv"])
        # token 不进 argv, 走 GIT_CONFIG_* 环境变量
        argv_str = " ".join(str(a) for a in seen["argv"])
        assert "t422" not in argv_str
        assert seen["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer t422"

    async def test_extend_reuses_existing_worktree(self, tmp_path):
        wt = tmp_path / "ai-pet-hotel"
        wt.mkdir()
        (wt / ".git").mkdir()
        node = RepoScaffoldNode()
        node.config.params = {
            "slug": "x",
            "base_dir": str(tmp_path),
            "verdict": "extend",
            "extend_repo": "o/ai-pet-hotel",
        }
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["extended"] is True
        assert res.data["repo_path"] == str(wt)

    async def test_new_verdict_scaffolds_normally(self, tmp_path):
        node = RepoScaffoldNode()
        node.config.params = {"slug": "fresh-422", "base_dir": str(tmp_path), "verdict": "new", "extend_repo": ""}
        res = await node.execute({})
        assert res.status.value == "success", res.error
        assert not res.data.get("extended")
        assert (tmp_path / "ai-fresh-422" / "pyproject.toml").exists()

    async def test_extend_needs_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        node = RepoScaffoldNode()
        node.config.params = {
            "slug": "x",
            "base_dir": str(tmp_path),
            "verdict": "extend",
            "extend_repo": "o/ai-pet-hotel",
        }
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "GITHUB_TOKEN" in res.error


class TestTemplateWiring:
    def test_match_node_wired(self):
        tpl = TemplateManager().get_template("product_lifecycle")
        nodes = {n["id"]: n for n in tpl["workflow"]["nodes"]}
        assert nodes["n4m"]["name"] == "historical_match"
        assert nodes["n5"]["config"]["params"]["verdict"] == "${step.n4m.verdict}"
        assert nodes["n5"]["config"]["params"]["extend_repo"] == "${step.n4m.matched_repo}"
        edges = {(e["source_id"], e["target_id"]) for e in tpl["workflow"]["edges"]}
        # issue #422 F-1: match 在闸门 B 之前自动运行 — 闸 B 暂停时判定卡可见, 可改判后批准
        assert ("n3", "n4m") in edges and ("n4m", "n4") in edges and ("n4m", "n5") in edges

    def test_new_params_exist(self):
        props = TemplateManager().get_template("product_lifecycle")["params"]["properties"]
        assert props["match_candidates"]["default"] == []
        assert props["match_threshold"]["default"] == 0.6
        assert props["own_customer"]["default"] == ""

    def test_assembly_defaults_resolve(self):
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        assert wf.nodes["n4m"].config.params["threshold"] == 0.6
        assert wf.nodes["n4m"].config.params["candidates"] == []
