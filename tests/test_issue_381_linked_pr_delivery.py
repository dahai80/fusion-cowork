"""issue #381: rerun deadlock — linked re-delivery when an existing open PR already covers the same scope.

- Session declares delivered_pr + PR open + body contains "Closes #N" -> legitimate delivery
  (outcome=linked_existing_pr), no longer requiring a new PR
- PR not open / body without closing link / not declared -> original strict failure path (falls back to state:ready)
- Dispatch prompt carries retry re-delivery guidance (no duplicate PR, link the existing PR)
"""

import json
import stat

import httpx

from fusion_cowork.nodes.ops.ops_nodes import ClaudeDispatchNode
from tests.test_issue_308_claude_dispatch import _fake_claude, _node


def _patch_api(monkeypatch, pr=None):
    monkeypatch.setenv("GITHUB_TOKEN", "t381")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/repos/org/repo/pulls/"):
            return httpx.Response(200, json=pr or {})
        if request.method == "POST" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={})
        if request.method == "DELETE" and "/labels/" in request.url.path:
            return httpx.Response(204, json={})
        if request.method == "GET" and request.url.path == "/repos/org/repo/issues/9":
            return httpx.Response(200, json={"state": "open", "number": 9})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    monkeypatch.setattr(
        ClaudeDispatchNode,
        "_api_client",
        lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
    )


def _result_json(text):
    return [f"echo '{json.dumps({'result': text, 'usage': {'input_tokens': 10, 'output_tokens': 10}})}'"]


class TestLinkedExistingPrDelivery:
    async def test_declared_open_closing_pr_passes(self, tmp_path, monkeypatch):
        _patch_api(
            monkeypatch,
            pr={"state": "open", "number": 18, "body": " redo of v0.5 scope\nCloses #9\n"},
        )
        out = "already delivered by PR #18, linked it to this issue. delivered_pr: https://github.com/org/repo/pull/18"
        binp = _fake_claude(tmp_path, _result_json(out))
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "success", res.error
        assert res.data["outcome"] == "linked_existing_pr"
        assert res.data["pr_url"] == "https://github.com/org/repo/pull/18"
        assert res.data["pr_number"] == 18
        assert "linked delivery" in res.summary

    async def test_pr_not_closing_issue_fails_back_to_ready(self, tmp_path, monkeypatch):
        _patch_api(monkeypatch, pr={"state": "open", "number": 18, "body": "no link here"})
        out = "delivered_pr: https://github.com/org/repo/pull/18"
        binp = _fake_claude(tmp_path, _result_json(out))
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert "校验未通过" in res.error  # i18n: user-facing

    async def test_closed_pr_fails(self, tmp_path, monkeypatch):
        _patch_api(monkeypatch, pr={"state": "closed", "number": 18, "body": "Closes #9"})
        out = "delivered_pr: https://github.com/org/repo/pull/18"
        binp = _fake_claude(tmp_path, _result_json(out))
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"

    async def test_undelivered_result_keeps_strict_path(self, tmp_path, monkeypatch):
        _patch_api(monkeypatch)
        binp = _fake_claude(tmp_path, _result_json("no pr this time"))
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert "会话未产出 PR" in res.error  # i18n: user-facing

    async def test_new_pr_delivery_unchanged(self, tmp_path, monkeypatch):
        _patch_api(monkeypatch)
        out = "done: https://github.com/org/repo/pull/21 with evidence"
        binp = _fake_claude(tmp_path, _result_json(out))
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "success", res.error
        assert "outcome" not in res.data
        assert res.data["pr_url"].endswith("/pull/21")


class TestRetryPromptGuidance:
    async def test_prompt_mentions_delivered_pr_contract(self, tmp_path, monkeypatch):
        _patch_api(monkeypatch)
        prompt_dump = tmp_path / "prompt.txt"
        # fake claude: dump the -p prompt arg ($2 after script path), then deliver linked PR
        script = tmp_path / "dump-claude.sh"
        script.write_text(
            "#!/bin/bash\nprintf '%s' \"$2\" > " + str(prompt_dump) + "\n"
            'echo \'{"result": "delivered_pr: https://github.com/org/repo/pull/18", '
            '"usage": {"input_tokens": 1, "output_tokens": 1}}\'\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        await _node(tmp_path, str(script)).execute({})
        prompt = prompt_dump.read_text()
        assert "delivered_pr" in prompt
        assert "Closes #" in prompt
        assert "do NOT open a duplicate PR" in prompt
