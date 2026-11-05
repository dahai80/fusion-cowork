"""issue #307: product_lifecycle template + repo_scaffold node.

Full delivery pipeline orchestration: PRD gate A -> arch gate B -> scaffold ->
task issue -> claude_dispatch -> linked-PR machine acceptance -> env/mock ->
gate C (batch verdicts) -> accepted + evidence close-out.
"""

import pytest

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.ops_nodes import RepoScaffoldNode
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager

import_all_nodes()


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


class TestProductLifecycleTemplate:
    def test_registered_12_nodes(self):
        nodes = _tpl()["workflow"]["nodes"]
        assert len(nodes) == 12

    def test_exactly_three_gates(self):
        gates = [n for n in _tpl()["workflow"]["nodes"] if n["name"] == "approval_gate"]
        assert [g["id"] for g in gates] == ["n2", "n4", "n10"]

    def test_stage_order_edges(self):
        pairs = {(e["source_id"], e["target_id"]) for e in _tpl()["workflow"]["edges"]}
        for a, b in (("n1", "n2"), ("n3", "n4"), ("n4", "n5"), ("n5", "n6"), ("n6", "n7"), ("n7", "n8"), ("n8", "n9"), ("n9", "n10"), ("n10", "n11"), ("n11", "n12")):
            assert (a, b) in pairs, (a, b)

    def test_data_wiring_edges(self):
        pairs = {(e["source_id"], e["target_id"]) for e in _tpl()["workflow"]["edges"]}
        # scaffold repo_path reaches dispatch; issue_create result reaches evidence/label nodes
        assert ("n5", "n7") in pairs
        assert ("n6", "n8") in pairs
        assert ("n6", "n11") in pairs
        assert ("n6", "n12") in pairs
        assert ("n8", "n12") in pairs

    def test_llm_seats_are_studio_run(self):
        studios = [n for n in _tpl()["workflow"]["nodes"] if n["name"] == "studio_run"]
        assert [n["id"] for n in studios] == ["n1", "n3", "n9"]

    def test_dispatch_contract_params(self):
        n7 = next(n for n in _tpl()["workflow"]["nodes"] if n["id"] == "n7")
        p = n7["config"]["params"]
        assert p["issue_number"] == "${result.number}"
        assert p["cwd"] == "${result.repo_path}"
        assert p["timeout"] == "${param.dispatch_timeout}"
        assert p["max_tokens"] == "${param.dispatch_max_tokens}"

    def test_task_issue_ready_label(self):
        n6 = next(n for n in _tpl()["workflow"]["nodes"] if n["id"] == "n6")
        assert n6["config"]["params"]["labels"] == ["state:ready"]

    def test_substitution(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle",
            param_values={
                "slug": "demo",
                "repo": "org/x",
                "task_title": "Do it",
                "ba_graph_id": "g-ba",
                "arch_graph_id": "g-arch",
                "env_graph_id": "g-env",
            },
        )
        assert wf is not None
        assert wf.nodes["n1"].config.params["graph_id"] == "g-ba"
        assert wf.nodes["n3"].config.params["graph_id"] == "g-arch"
        assert wf.nodes["n5"].config.params["slug"] == "demo"
        assert wf.nodes["n6"].config.params["repo"] == "org/x"
        assert wf.nodes["n6"].config.params["title"] == "Do it"


class TestRepoScaffold:
    @pytest.mark.asyncio
    async def test_creates_skeleton_and_git(self, tmp_path):
        node = RepoScaffoldNode()
        node.config.params = {"slug": "demo", "base_dir": str(tmp_path)}
        res = await node.execute({})
        assert res.status.value == "success"
        rp = res.data["repo_path"]
        assert rp.endswith("ai-demo")
        for rel in ("pyproject.toml", "CLAUDE.md", "README.md", "README_CN.md", ".python-version"):
            assert rel in res.data["created_files"]
        assert (tmp_path / "ai-demo" / ".git").is_dir()
        assert "fusion_ai_demo" in (tmp_path / "ai-demo" / "pyproject.toml").read_text()

    @pytest.mark.asyncio
    async def test_refuses_existing_dir(self, tmp_path):
        (tmp_path / "ai-demo").mkdir()
        node = RepoScaffoldNode()
        node.config.params = {"slug": "demo", "base_dir": str(tmp_path)}
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "已存在" in res.error

    @pytest.mark.asyncio
    async def test_rejects_bad_slug(self, tmp_path):
        node = RepoScaffoldNode()
        node.config.params = {"slug": "Bad Slug", "base_dir": str(tmp_path)}
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "slug" in res.error

    @pytest.mark.asyncio
    async def test_missing_base_dir(self, tmp_path):
        node = RepoScaffoldNode()
        node.config.params = {"slug": "demo", "base_dir": str(tmp_path / "nope")}
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "base_dir" in res.error
