import httpx
import pytest

from fusion_cowork.engine.node import (
    BaseNode,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Edge, Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager


@pytest.fixture(autouse=True)
def _import_all_nodes():
    import_all_nodes()


@register_node
class ProbeRunIdNode283(BaseNode):
    name = "probe_run_id_283"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"seen": inputs.get("run_id")})


class TestRunIdInjection:
    @pytest.mark.asyncio
    async def test_nodes_see_run_id(self):
        eng = WorkflowEngine()
        wf = Workflow(name="wf283")
        node = NodeRegistry.create("probe_run_id_283", node_id="probe", config=NodeConfig())
        wf.add_node(node)
        execution = await eng.execute(wf, execution_id="run_283_probe")
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].output_data["seen"] == "run_283_probe"

    @pytest.mark.asyncio
    async def test_upstream_value_wins_over_injection(self):
        @register_node
        class EmitRunIdNode283(BaseNode):
            name = "emit_run_id_283"

            async def execute(self, inputs):
                return NodeResult(status=NodeStatus.SUCCESS, data={"run_id": "from-upstream"})

        eng = WorkflowEngine()
        wf = Workflow(name="wf283b")
        emit = NodeRegistry.create("emit_run_id_283", node_id="emit", config=NodeConfig())
        sink = NodeRegistry.create("probe_run_id_283", node_id="sink", config=NodeConfig())
        wf.add_node(emit)
        wf.add_node(sink)
        wf.edges.append(Edge(source_id="emit", target_id="sink"))
        execution = await eng.execute(wf, execution_id="run_283_up")
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[-1].output_data["seen"] == "from-upstream"


class TestIssueListOp:
    @pytest.mark.asyncio
    async def test_issue_list_builds_label_query(self, monkeypatch):
        from fusion_cowork.nodes.ops.ops_nodes import GitHubNode

        monkeypatch.setenv("GITHUB_TOKEN", "tok283")
        seen_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_path["p"] = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
            return httpx.Response(200, json=[{"number": 7, "title": "i7"}])

        def fake_client(self, base_url, token, timeout):
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(GitHubNode, "_client", fake_client)
        node = GitHubNode(
            "gh",
            NodeConfig(
                params={
                    "op": "issue_list",
                    "repo": "org/repo",
                    "allowed_repos": ["org/repo"],
                    "labels": ["state:approved"],
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert seen_path["p"] == "/repos/org/repo/issues?labels=state:approved"
        assert result.data["result"][0]["number"] == 7

    @pytest.mark.asyncio
    async def test_issue_list_no_labels_bare_path(self, monkeypatch):
        from fusion_cowork.nodes.ops.ops_nodes import GitHubNode

        monkeypatch.setenv("GITHUB_TOKEN", "tok283")
        seen_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_path["p"] = request.url.path
            return httpx.Response(200, json=[])

        def fake_client(self, base_url, token, timeout):
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(GitHubNode, "_client", fake_client)
        node = GitHubNode(
            "gh", NodeConfig(params={"op": "issue_list", "repo": "org/repo", "allowed_repos": ["org/repo"]})
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert seen_path["p"] == "/repos/org/repo/issues"


class TestGovernanceTemplates:
    def test_templates_registered(self):
        mgr = TemplateManager()
        ids = [t["id"] for t in mgr.list_templates()]
        assert "governance_issue_lifecycle" in ids
        assert "governance_batch_audit" in ids

    def test_lifecycle_converts_and_nodes_exist(self):
        mgr = TemplateManager()
        wf = mgr.template_to_workflow("governance_issue_lifecycle")
        assert wf is not None
        names = [n.name for n in wf.nodes.values()]
        assert names.count("approval_gate") == 3
        assert "github_api" in names
        assert "http_request" in names

    def test_batch_converts(self):
        mgr = TemplateManager()
        wf = mgr.template_to_workflow("governance_batch_audit")
        assert wf is not None
        assert next(n.name for n in wf.nodes.values()) == "github_api"

    def test_lifecycle_payload_carries_run_id_template(self):
        # issue #293: run context nested under payload — agent-studio persists only
        # body["payload"] as task params, top-level run_id gets dropped
        # issue #294: G1/G5 sync nodes added — all three gates carry nested payloads
        tpl = next(t for t in BUILTIN_TEMPLATES if t["id"] == "governance_issue_lifecycle")
        by_id = {n["id"]: n for n in tpl["workflow"]["nodes"]}
        for nid, gate, kind in (("n1b", "G1", "proposal"), ("n4", "G4", "acceptance"), ("n6b", "G5", "rollout")):
            payload = by_id[nid]["config"]["params"]["body"]["payload"]
            assert payload["run_id"] == "${run_id}"
            assert payload["gate"] == gate
            assert payload["kind"] == kind
