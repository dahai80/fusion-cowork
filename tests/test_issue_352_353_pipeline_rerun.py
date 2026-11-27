"""issue #352/#353: pipeline rerun idempotency + claude_dispatch permission mode.

- #352: repo_scaffold reuses an empty dir / overwrite rewrites in place / failure errors
  carry a remediation hint; github_api issue_create idempotently reuses an open issue
  with the same title instead of recreating
- #353: claude_dispatch defaults to --permission-mode bypassPermissions (under headless -p,
  approval-requiring tools are auto-denied, making the delivery contract structurally unreachable)
"""

import stat

import httpx

from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode, GitHubNode, RepoScaffoldNode
from tests.test_issue_308_claude_dispatch import _OK_RESULT


def _scaffold_node(slug="probe", base_dir=None, **extra):
    node = RepoScaffoldNode()
    node.config.params = {"slug": slug, "base_dir": base_dir, **extra}
    return node


class TestRepoScaffoldIdempotent:
    async def test_empty_dir_reused(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        target = base / "ai-probe"
        target.mkdir()  # empty dir (leftover from an earlier failure)
        res = await _scaffold_node(base_dir=str(base)).execute({})
        assert res.status.value == "success"
        assert res.data["reused"] is True
        assert res.data["repo_path"] == str(target)
        assert (target / "pyproject.toml").exists()

    async def test_existing_content_fails_with_remediation_hint(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        target = base / "ai-probe"
        target.mkdir()
        (target / "leftover.txt").write_text("x")
        res = await _scaffold_node(base_dir=str(base)).execute({})
        assert res.status.value == "failed"
        assert "overwrite=true" in res.error

    async def test_overwrite_rewrites_in_place(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        target = base / "ai-probe"
        target.mkdir()
        (target / "stale.txt").write_text("stale")
        res = await _scaffold_node(base_dir=str(base), overwrite=True).execute({})
        assert res.status.value == "success"
        assert res.data["reused"] is False
        assert (target / "pyproject.toml").exists()
        assert (target / "stale.txt").exists()  # rewrites the scaffold, does not clear existing output

    async def test_fresh_scaffold_still_works(self, tmp_path):
        base = tmp_path / "biz"
        base.mkdir()
        res = await _scaffold_node(base_dir=str(base)).execute({})
        assert res.status.value == "success"
        assert res.data["reused"] is False
        assert res.data["repo_path"]


def _api_node(op, **params):
    node = GitHubNode()
    node.config.params = {"op": op, "repo": "o/r", "allowed_repos": ["o/r"], **params}
    return node


class TestIssueCreateDedup:
    async def test_same_title_open_issue_reused_not_recreated(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t352")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, str(request.url.params.get("q", ""))))
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"number": 7, "title": "Do the thing", "html_url": "https://github.com/o/r/issues/7"}]
                    },
                )
            raise AssertionError(f"issue_create must not POST when dedup hits: {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        res = await _api_node("issue_create", title="Do the thing", body="b").execute({})
        assert res.status.value == "success"
        assert res.data["deduplicated"] is True
        assert res.data["result"]["number"] == 7
        assert calls[0][0] == "GET" and calls[0][1] == "/search/issues"

    async def test_no_match_creates_issue(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t352")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                return httpx.Response(200, json={"items": [{"number": 7, "title": "Unrelated"}]})
            if request.url.path == "/repos/o/r/issues" and request.method == "POST":
                return httpx.Response(201, json={"number": 9, "title": "Do the thing", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        res = await _api_node("issue_create", title="Do the thing", body="b").execute({})
        assert res.status.value == "success"
        assert res.data.get("deduplicated", False) is False
        assert res.data["result"]["number"] == 9


def _dispatch_node(tmp_path, claude_bin, **extra):
    node = ClaudeDispatchNode()
    node.config.params = {
        "repo": "org/repo",
        "allowed_repos": ["org/repo"],
        "issue_number": 5,
        "prompt": "p",
        "cwd": str(tmp_path),
        "claude_bin": claude_bin,
        **extra,
    }
    return node


class TestPermissionMode:
    async def test_default_bypass_permissions_flag_sent(self, tmp_path):
        argv_file = tmp_path / "argv.txt"
        probe = tmp_path / "probe.sh"
        probe.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + str(argv_file) + f"\necho '{_OK_RESULT}'\n")
        probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
        res = await _dispatch_node(tmp_path, str(probe)).execute({})
        assert res.status.value == "success", res.error
        argv = argv_file.read_text().splitlines()
        assert "--permission-mode" in argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    async def test_permission_mode_param_override(self, tmp_path):
        argv_file = tmp_path / "argv.txt"
        probe = tmp_path / "probe.sh"
        probe.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + str(argv_file) + f"\necho '{_OK_RESULT}'\n")
        probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
        res = await _dispatch_node(tmp_path, str(probe), permission_mode="default").execute({})
        assert res.status.value == "success", res.error
        argv = argv_file.read_text().splitlines()
        assert argv[argv.index("--permission-mode") + 1] == "default"

    def test_schema_default_is_bypass(self):
        assert (
            ClaudeDispatchNode().get_params_schema()["properties"]["permission_mode"]["default"] == "bypassPermissions"
        )
