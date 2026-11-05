"""issue #306: G4 PR-existence precondition (deterministic evidence gate).

github_api gains the linked_pr op (pure REST read, zero LLM): scan the issue
timeline for cross-referenced events whose source issue is a PR; require=true
fails the node with the explicit block reason so G4 never attests work that
does not exist (resume-from-step re-runs the check). CI status is best-effort.
The lifecycle template wires n4a (linked_pr, require) before the G4 sync and
stamps pr_url/ci_status into the sync payload evidence block.
"""

import asyncio
from unittest import mock

import httpx
import pytest

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.ops_nodes import GitHubNode

import_all_nodes()


def _mock_client(handler):
    def _factory(self, base_url, token, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)

    return _factory


TIMELINE_WITH_PR = [
    {"event": "commented"},
    {
        "event": "cross-referenced",
        "source": {
            "issue": {"number": 42, "html_url": "https://github.com/org/x/pull/42", "pull_request": {"url": "x"}}
        },
    },
]

TIMELINE_NO_PR = [{"event": "commented"}, {"event": "cross-referenced", "source": {"issue": {"number": 7}}}]


def _handler(paths):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/timeline"):
            return httpx.Response(200, json=paths.get("timeline", []))
        if "/pulls/" in path:
            return httpx.Response(200, json={"head": {"sha": "abc123"}})
        if "/status" in path:
            return httpx.Response(200, json={"state": "success"})
        return httpx.Response(404, json={"message": "not found"})

    return handle


def _run_node(op_args: dict, paths: dict) -> dict:
    node = GitHubNode(
        node_id="n", config=type("C", (), {"params": {**op_args, "allowed_repos": ["org/x"]}, "label": ""})()
    )
    with mock.patch.object(GitHubNode, "_client", _mock_client(_handler(paths))):
        res = asyncio.run(node.execute({}))
    return res


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    monkeypatch.delenv("FUSION_GITHUB_API", raising=False)


class TestLinkedPrOp:
    def test_found_pr_reports_url_number_ci(self):
        res = _run_node({"op": "linked_pr", "repo": "org/x", "issue_number": 5}, {"timeline": TIMELINE_WITH_PR})
        assert res.status.value == "success"
        assert res.data["result"] == {
            "found": True,
            "pr_url": "https://github.com/org/x/pull/42",
            "pr_number": 42,
            "ci_status": "success",
        }

    def test_no_pr_reports_found_false(self):
        res = _run_node({"op": "linked_pr", "repo": "org/x", "issue_number": 5}, {"timeline": TIMELINE_NO_PR})
        assert res.status.value == "success"
        assert res.data["result"]["found"] is False
        assert res.data["result"]["pr_url"] == ""

    def test_require_true_blocks_without_pr(self):
        res = _run_node(
            {"op": "linked_pr", "repo": "org/x", "issue_number": 5, "require": True}, {"timeline": TIMELINE_NO_PR}
        )
        assert res.status.value == "failed"
        assert "先提 PR 再进验收" in res.error
        assert res.data["found"] is False

    def test_require_true_passes_with_pr(self):
        res = _run_node(
            {"op": "linked_pr", "repo": "org/x", "issue_number": 5, "require": True}, {"timeline": TIMELINE_WITH_PR}
        )
        assert res.status.value == "success"
        assert res.data["result"]["pr_number"] == 42

    def test_ci_status_failure_does_not_block(self):
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/timeline"):
                return httpx.Response(200, json=TIMELINE_WITH_PR)
            return httpx.Response(500, json={})

        node = GitHubNode(
            node_id="n",
            config=type(
                "C",
                (),
                {
                    "params": {"op": "linked_pr", "repo": "org/x", "issue_number": 5, "allowed_repos": ["org/x"]},
                    "label": "",
                },
            )(),
        )
        with mock.patch.object(GitHubNode, "_client", _mock_client(handle)):
            res = asyncio.run(node.execute({}))
        assert res.status.value == "success"
        assert res.data["result"]["ci_status"] == ""

    def test_missing_issue_number_fails(self):
        res = _run_node({"op": "linked_pr", "repo": "org/x"}, {"timeline": []})
        assert res.status.value == "failed"


class TestTemplateWiring:
    def test_lifecycle_has_n4a_before_g4_sync(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle", param_values={"repo": "org/x", "issue_number": 1}
        )
        ids = list(wf.nodes.keys())
        assert "n4a" in ids
        assert ids.index("n3") < ids.index("n4a") < ids.index("n4") < ids.index("n5")
        n4a = wf.nodes["n4a"]
        assert n4a.config.params["op"] == "linked_pr"
        assert n4a.config.params["require"] is True

    def test_g4_sync_payload_carries_evidence_placeholders(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle", param_values={"repo": "org/x", "issue_number": 1}
        )
        payload = wf.nodes["n4"].config.params["body"]["payload"]
        assert payload["evidence"] == {"pr_url": "${result.pr_url}", "ci_status": "${result.ci_status}"}

    def test_edges_route_through_n4a(self):
        from fusion_cowork.templates.template_manager import TemplateManager

        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle", param_values={"repo": "org/x", "issue_number": 1}
        )
        pairs = {(e.source_id, e.target_id) for e in wf.edges}
        assert ("n3", "n4a") in pairs and ("n4a", "n4") in pairs
        assert ("n3", "n4") not in pairs
