"""issue #361/#362: pipeline re-run parameter pass-through + PR link strength contract.

- #361: product_lifecycle template exposes a scaffold_overwrite launch parameter, passed through to the n5 repo_scaffold
  overwrite; issue_create dedup reuse (v0.5.85) downstream reuse via ${result.number} unchanged
- #362: linked_pr adds pr_link_strength (closing|mention) — only a PR body with closing keywords counts as a
  delivery-grade link; require_closing_link=true blocks mention-only; claude_dispatch output carries the session
  self-declared delivery claim session_claim
"""

import json
import stat

import httpx

from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode, GitHubNode
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager
from tests.test_issue_308_claude_dispatch import _OK_RESULT


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


class TestTemplateOverwriteThreading:
    def test_scaffold_overwrite_param_declared(self):
        assert _tpl()["params"]["properties"]["scaffold_overwrite"]["default"] is False

    def test_n5_receives_overwrite(self):
        n5 = next(n for n in _tpl()["workflow"]["nodes"] if n["id"] == "n5")
        assert n5["config"]["params"]["overwrite"] == "${param.scaffold_overwrite}"

    def test_substitution_preserves_bool(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle",
            param_values={"slug": "demo", "repo": "org/x", "task_title": "t", "scaffold_overwrite": True},
        )
        assert wf.nodes["n5"].config.params["overwrite"] is True

    def test_default_substitution_false(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "org/x", "task_title": "t"}
        )
        assert wf.nodes["n5"].config.params["overwrite"] is False


class TestTemplateClosingLink:
    def test_require_closing_link_param_default_true(self):
        assert _tpl()["params"]["properties"]["require_closing_link"]["default"] is True

    def test_n8_receives_require_closing_link(self):
        n8 = next(n for n in _tpl()["workflow"]["nodes"] if n["id"] == "n8")
        assert n8["config"]["params"]["require_closing_link"] == "${param.require_closing_link}"

    def test_task_prompt_demands_closing_keyword(self):
        assert "Closes" in _tpl()["params"]["properties"]["task_prompt"]["default"]


def _api_node(op, **params):
    node = GitHubNode()
    node.config.params = {"op": op, "repo": "o/r", "allowed_repos": ["o/r"], "issue_number": 8, **params}
    return node


def _gh_node(pr_body):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/o/r/issues/8/timeline":
            return httpx.Response(
                200,
                json=[
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "number": 9,
                                "html_url": "https://github.com/o/r/pull/9",
                                "pull_request": {"url": "x"},
                            }
                        },
                    }
                ],
            )
        if path == "/repos/o/r/pulls/9":
            return httpx.Response(200, json={"head": {"sha": "abc123"}, "body": pr_body})
        if path == "/repos/o/r/commits/abc123/status":
            return httpx.Response(200, json={"state": "success"})
        raise AssertionError(f"unexpected {request.method} {path}")

    return lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to)


class TestPrLinkStrength:
    async def test_closing_keyword_detected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t362")
        monkeypatch.setattr(GitHubNode, "_client", _gh_node("Implements the scope. Closes #8"))
        res = await _api_node("linked_pr").execute({})
        assert res.status.value == "success"
        assert res.data["result"]["pr_link_strength"] == "closing"
        assert res.data["result"]["pr_number"] == 9

    async def test_mention_only_flagged(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t362")
        monkeypatch.setattr(GitHubNode, "_client", _gh_node("Found while working on #8, chore cleanup"))
        res = await _api_node("linked_pr").execute({})
        assert res.status.value == "success"
        assert res.data["result"]["pr_link_strength"] == "mention"

    async def test_require_closing_link_blocks_mention(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t362")
        monkeypatch.setattr(GitHubNode, "_client", _gh_node("Found while working on #8"))
        res = await _api_node("linked_pr", require_closing_link=True).execute({})
        assert res.status.value == "failed"
        # i18n: user-facing
        assert "关闭关键词" in res.error
        assert "PR#9" in res.error
        assert res.data["pr_link_strength"] == "mention"

    async def test_require_closing_link_passes_closing(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t362")
        monkeypatch.setattr(GitHubNode, "_client", _gh_node("fixes #8 properly"))
        res = await _api_node("linked_pr", require_closing_link=True).execute({})
        assert res.status.value == "success"
        assert res.data["result"]["pr_link_strength"] == "closing"

    async def test_pr_fetch_failure_degrades_mention(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t362")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/repos/o/r/issues/8/timeline":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 9,
                                    "html_url": "https://github.com/o/r/pull/9",
                                    "pull_request": {"url": "x"},
                                }
                            },
                        }
                    ],
                )
            if path == "/repos/o/r/pulls/9":
                return httpx.Response(500)
            if path.endswith("/status"):
                return httpx.Response(500)
            raise AssertionError(f"unexpected {request.method} {path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        res = await _api_node("linked_pr", require_closing_link=True).execute({})
        assert res.status.value == "failed"
        assert res.data["pr_link_strength"] == "mention"

    def test_no_closing_regex_false_positive(self):
        # "Closes #8" does not match "Closes #88" — word-boundary validation (\\b built into the pattern)
        import re

        body = "Closes #88 not this one"
        assert not re.search(r"\b(?:close[sd]?|fix(?:e[sd]?)?|resolve[sd]?)\s*:?\s*#8\b", body, re.IGNORECASE)


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


class TestSessionClaim:
    async def test_success_output_carries_session_claim(self, tmp_path):
        argv_file = tmp_path / "argv.txt"
        probe = tmp_path / "probe.sh"
        probe.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + str(argv_file) + f"\necho '{_OK_RESULT}'\n")
        probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
        res = await _dispatch_node(tmp_path, str(probe)).execute({})
        assert res.status.value == "success", res.error
        claim = res.data["session_claim"]
        assert claim
        assert "PR https://github.com/org/repo/pull/42" in claim
        parsed = json.loads(_OK_RESULT)
        assert claim.startswith(parsed["result"][:20])
