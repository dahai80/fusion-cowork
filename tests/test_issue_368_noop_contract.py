"""issue #368: n6 dedupe 全状态 + scaffold overwrite 非破坏性 + n7 no-op 交付契约。

- github_api issue_create 去重查 open+closed (PR 合并关闭后重跑不再建假任务)
- repo_scaffold overwrite 只补缺失脚手架文件, 不覆写既有文件 (交付内容不被打回 stub)
- claude_dispatch 会话关闭 issue 为 duplicate/invalid (无 PR) = 合法 no-op 交付
- linked_pr: issue 已关闭且无关联 PR 放行验收 (no-op 交付)
"""

import json

import httpx

from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode, GitHubNode, RepoScaffoldNode
from tests.test_issue_308_claude_dispatch import _fake_claude
from tests.test_issue_308_claude_dispatch import _node as _dispatch_node


def _scaffold_node(slug="probe", base_dir=None, **extra):
    node = RepoScaffoldNode()
    node.config.params = {"slug": slug, "base_dir": base_dir, **extra}
    return node


def _api_node(op, **params):
    node = GitHubNode()
    node.config.params = {"op": op, "repo": "o/r", "allowed_repos": ["o/r"], **params}
    return node


def _patch_gh_client(monkeypatch, handler):
    monkeypatch.setenv("GITHUB_TOKEN", "t368")
    monkeypatch.setattr(
        GitHubNode,
        "_client",
        lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
    )


class TestIssueCreateDedupAllStates:
    async def test_closed_same_title_issue_reused_not_recreated(self, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "number": 5,
                                "title": "Implement v0.1 probe scope",
                                "state": "closed",
                                "html_url": "https://github.com/o/r/issues/5",
                            }
                        ]
                    },
                )
            raise AssertionError(f"issue_create must not POST when dedup hits: {request.method} {request.url.path}")

        _patch_gh_client(monkeypatch, handler)
        res = await _api_node("issue_create", title="Implement v0.1 probe scope", body="b").execute({})
        assert res.status.value == "success"
        assert res.data["deduplicated"] is True
        assert res.data["state"] == "closed"
        assert res.data["result"]["number"] == 5
        assert calls == [("GET", "/search/issues")]

    async def test_no_match_still_creates(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                return httpx.Response(200, json={"items": []})
            if request.url.path == "/repos/o/r/issues" and request.method == "POST":
                return httpx.Response(201, json={"number": 11, "title": "t", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        _patch_gh_client(monkeypatch, handler)
        res = await _api_node("issue_create", title="t", body="b").execute({})
        assert res.status.value == "success"
        assert res.data["result"]["number"] == 11


class TestScaffoldOverwriteNonDestructive:
    async def test_existing_delivered_files_kept_missing_filled(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        target = base / "ai-probe"
        target.mkdir()
        # 已交付内容 — 与脚手架 stub 不同, 必须原样保留
        delivered_readme = "# ai-probe v0.1\n\ndelivered docs\n"
        delivered_pyproject = '[project]\nname = "fusion_ai_probe"\nversion = "0.2.0"\n'
        (target / "README.md").write_text(delivered_readme)
        (target / "pyproject.toml").write_text(delivered_pyproject)
        (target / "src" / "pkg").mkdir(parents=True)
        res = await _scaffold_node(base_dir=str(base), overwrite=True).execute({})
        assert res.status.value == "success"
        assert (target / "README.md").read_text() == delivered_readme
        assert (target / "pyproject.toml").read_text() == delivered_pyproject
        # 缺失脚手架文件补齐
        assert (target / "CLAUDE.md").exists()
        assert (target / "src" / "pkg").exists()
        assert set(res.data["skipped_files"]) == {"README.md", "pyproject.toml"}
        assert "CLAUDE.md" in res.data["created_files"]

    async def test_non_empty_without_overwrite_still_fails(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        target = base / "ai-probe"
        target.mkdir()
        (target / "x.txt").write_text("x")
        res = await _scaffold_node(base_dir=str(base)).execute({})
        assert res.status.value == "failed"
        assert "overwrite=true" in res.error


def _patch_dispatch_client(monkeypatch, issue_state):
    monkeypatch.setenv("GITHUB_TOKEN", "t368")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/org/repo/issues/9":
            return httpx.Response(200, json={"state": issue_state, "number": 9})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        ClaudeDispatchNode,
        "_api_client",
        lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
    )


class TestDispatchNoopDelivery:
    async def test_issue_closed_no_pr_is_success(self, tmp_path, monkeypatch):
        _patch_dispatch_client(monkeypatch, "closed")
        no_pr = json.dumps({"result": "#9 duplicate of #5, closed with evidence", "usage": {"input_tokens": 10, "output_tokens": 10}})
        binp = _fake_claude(tmp_path, [f"echo '{no_pr}'"])
        res = await _dispatch_node(tmp_path, binp).execute({})
        assert res.status.value == "success", res.error
        assert res.data["outcome"] == "closed_no_pr"
        assert res.data["pr_url"] == ""
        assert "duplicate" in res.data["session_claim"]

    async def test_issue_open_no_pr_still_fails_back_to_ready(self, tmp_path, monkeypatch):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.method == "GET" and request.url.path == "/repos/org/repo/issues/9":
                return httpx.Response(200, json={"state": "open", "number": 9})
            return httpx.Response(200, json={})

        monkeypatch.setenv("GITHUB_TOKEN", "t368")
        monkeypatch.setattr(
            ClaudeDispatchNode,
            "_api_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        no_pr = json.dumps({"result": "no pr produced", "usage": {"input_tokens": 10, "output_tokens": 10}})
        binp = _fake_claude(tmp_path, [f"echo '{no_pr}'"])
        res = await _dispatch_node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert "PR" in res.error
        assert ("POST", "/repos/org/repo/issues/9/labels") in requests


class TestLinkedPrClosedNoop:
    async def test_closed_issue_without_pr_passes(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/o/r/issues/5/timeline":
                return httpx.Response(200, json=[])
            if request.url.path == "/repos/o/r/issues/5":
                return httpx.Response(200, json={"state": "closed", "number": 5})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        _patch_gh_client(monkeypatch, handler)
        res = await _api_node("linked_pr", issue_number=5, require=True).execute({})
        assert res.status.value == "success"
        assert res.data["result"]["issue_closed"] is True
        assert res.data["result"]["found"] is False

    async def test_open_issue_without_pr_still_blocks(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/o/r/issues/5/timeline":
                return httpx.Response(200, json=[])
            if request.url.path == "/repos/o/r/issues/5":
                return httpx.Response(200, json={"state": "open", "number": 5})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        _patch_gh_client(monkeypatch, handler)
        res = await _api_node("linked_pr", issue_number=5, require=True).execute({})
        assert res.status.value == "failed"
        assert "先提 PR" in res.error

    async def test_state_fetch_failure_still_blocks(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/o/r/issues/5/timeline":
                return httpx.Response(200, json=[])
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        _patch_gh_client(monkeypatch, handler)
        res = await _api_node("linked_pr", issue_number=5, require=True).execute({})
        assert res.status.value == "failed"
