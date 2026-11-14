"""issue #480: P8 mandatory verification node (deliverable vs top-level objective).

- verify_objective node: keyword strategy (deterministic substring rubric), min_ratio,
  on_fail fail/flag, missing-param loud errors, llm strategy judge parse (mocked chat)
- Workflow validation: terminal deliverables without a verify_objective predecessor —
  strict = validation error, lenient = warning only, off = silent; verified terminals pass
- Engine journal feed: verify_objective result recorded in ApprovalJournal
  (rubric + verdict as a decided row)
"""

from __future__ import annotations

from fusion_cowork.engine.approval_journal import ApprovalJournal
from fusion_cowork.engine.node import NodeRegistry
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.verify_node import VerifyObjectiveNode

import_all_nodes()


def _verify(params: dict) -> VerifyObjectiveNode:
    n = NodeRegistry.create("verify_objective", node_id="nv")
    n.config.params = params
    return n


class TestVerifyNode:
    async def test_keyword_all_pass(self):
        res = await _verify(
            {
                "objective": "ship PRD",
                "deliverable": "The PRD covers scope, milestones and success metrics.",
                "rubric": ["prd", "scope", "milestones"],
            }
        ).execute({})
        assert res.status.value == "success"
        assert res.data["passed"] is True
        assert res.data["ratio"] == 1.0
        assert all(r["passed"] for r in res.data["rubric_results"])

    async def test_keyword_case_insensitive(self):
        res = await _verify({"objective": "o", "deliverable": "UPPER CASE REPORT", "rubric": ["upper case"]}).execute(
            {}
        )
        assert res.data["passed"] is True

    async def test_keyword_failure_fails_node(self):
        res = await _verify({"objective": "o", "deliverable": "incomplete text", "rubric": ["missing-token"]}).execute(
            {}
        )
        assert res.status.value == "failed"
        assert res.data["passed"] is False
        assert "missing-token" in str([r["item"] for r in res.data["rubric_results"]])

    async def test_min_ratio_threshold(self):
        params = {"objective": "o", "deliverable": "has alpha only", "rubric": ["alpha", "beta"], "min_ratio": 0.5}
        res = await _verify(params).execute({})
        assert res.status.value == "success"
        assert res.data["ratio"] == 0.5
        params["min_ratio"] = 0.6
        res2 = await _verify(params).execute({})
        assert res2.status.value == "failed"

    async def test_on_fail_flag(self):
        res = await _verify({"objective": "o", "deliverable": "text", "rubric": ["nope"], "on_fail": "flag"}).execute(
            {}
        )
        assert res.status.value == "success"
        assert res.data["passed"] is False

    async def test_missing_rubric_loud(self):
        res = await _verify({"objective": "o", "deliverable": "d"}).execute({})
        assert res.status.value == "failed"
        assert "rubric" in res.error

    async def test_missing_objective_loud(self):
        res = await _verify({"deliverable": "d", "rubric": ["x"]}).execute({})
        assert res.status.value == "failed"
        assert "objective" in res.error

    async def test_missing_deliverable_loud(self):
        res = await _verify({"objective": "o", "rubric": ["x"]}).execute({})
        assert res.status.value == "failed"
        assert "deliverable" in res.error

    async def test_inputs_fallback(self):
        node = _verify({"rubric": ["value-480"]})
        res = await node.execute({"objective": "obj-480", "deliverable": "carries value-480 inside"})
        assert res.status.value == "success"
        assert res.data["objective"] == "obj-480"

    async def test_llm_strategy_parses_verdict(self, monkeypatch):
        class _FakeResp:
            content = (
                'Here you go: {"results": [{"item": "has prd", "passed": true, "evidence": "yes"},'
                '{"item": "has budget", "passed": false, "evidence": "absent"}]}'
            )

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def chat(self, model=None, messages=None, **kw):
                return _FakeResp()

        import fusion_cowork.ai.mlx_client as mlx_mod

        monkeypatch.setattr(mlx_mod, "FusionMLXClient", _FakeClient)
        res = await _verify(
            {
                "objective": "o",
                "deliverable": "d",
                "rubric": ["has prd", "has budget"],
                "strategy": "llm",
                "llm_model": "fake",
                "min_ratio": 0.5,
            }
        ).execute({})
        assert res.status.value == "success"
        assert res.data["ratio"] == 0.5
        assert res.data["rubric_results"][1]["passed"] is False
        assert res.data["rubric_results"][1]["evidence"] == "absent"


def _wf_two_nodes(policy: str = "strict", with_verify: bool = False) -> Workflow:
    wf = Workflow(name="wf-480", verification_policy=policy)
    produce = NodeRegistry.create("merge", node_id="np")
    produce.config.params = {}
    wf.add_node(produce)
    if with_verify:
        v = NodeRegistry.create("verify_objective", node_id="nv")
        v.config.params = {
            "objective": "o",
            "rubric": ["ok"],
            "deliverable": "all ok",
        }
        wf.add_node(v)
        wf.connect("np", "nv")
    return wf


class TestValidationRule:
    def test_strict_rejects_unverified_terminal(self):
        wf = _wf_two_nodes(policy="strict")
        errors = wf.validate()
        assert any("verify_objective" in e and "np" in e for e in errors)

    def test_strict_accepts_verified_terminal(self):
        wf = _wf_two_nodes(policy="strict", with_verify=True)
        assert wf.validate() == []

    def test_lenient_passes_with_warning(self, caplog):
        wf = _wf_two_nodes(policy="lenient")
        assert wf.validate() == []

    def test_off_silent(self):
        wf = _wf_two_nodes(policy="off")
        assert wf.validate() == []

    def test_verify_as_terminal_passes_strict(self):
        # 校验节点本身就是终端交付节点 — 合法
        wf = Workflow(name="wf-480b", verification_policy="strict")
        v = NodeRegistry.create("verify_objective", node_id="nv")
        v.config.params = {"objective": "o", "rubric": ["ok"], "deliverable": "ok"}
        wf.add_node(v)
        assert wf.validate() == []

    def test_policy_roundtrip(self):
        wf = _wf_two_nodes(policy="strict", with_verify=True)
        wf2 = Workflow.from_dict(wf.to_dict())
        assert wf2.verification_policy == "strict"
        assert wf2.validate() == []


class TestJournalFeed:
    def test_journal_closed_on_exit(self, tmp_path):
        journal = ApprovalJournal(path=tmp_path / "j480.db")
        journal.close()

    async def _run_graph(self, tmp_path, rubric_pass: bool):
        journal = ApprovalJournal(path=tmp_path / "j480run.db")
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False, approval_journal=journal)
        wf = Workflow(name="wf-480j", verification_policy="off")
        produce = NodeRegistry.create("merge", node_id="np")
        produce.config.params = {}
        wf.add_node(produce)
        v = NodeRegistry.create("verify_objective", node_id="nv")
        deliverable = "contains ok-marker" if rubric_pass else "does not contain it"
        v.config.params = {"objective": "obj-480", "rubric": ["ok-marker"], "deliverable": deliverable}
        wf.add_node(v)
        wf.connect("np", "nv")
        ex = await eng.execute(wf, execution_id="run_480_j")
        return journal, ex

    async def test_pass_recorded(self, tmp_path):
        journal, ex = await self._run_graph(tmp_path, rubric_pass=True)
        try:
            assert ex.status.value == "success"
            row = journal.decision_for("run_480_j", "nv")
            assert row is not None
            assert row["approved"] == 1
            assert "verify_objective" in (row["comments"] or "") or row["comments"]
            import json

            params = json.loads(row["params"] or "{}")
            assert params["objective"] == "obj-480"
            assert params["rubric"] == ["ok-marker"]
        finally:
            journal.close()

    async def test_fail_recorded_and_run_fails(self, tmp_path):
        journal, ex = await self._run_graph(tmp_path, rubric_pass=False)
        try:
            assert ex.status.value == "failed"
            row = journal.decision_for("run_480_j", "nv")
            assert row is not None
            assert row["approved"] == 0
        finally:
            journal.close()

    def test_node_registered_and_aliases(self):
        assert NodeRegistry.get("verify_objective") is not None
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES.get("目标校验") == "verify_objective"
        assert NODE_NAME_ALIASES.get("交付验证") == "verify_objective"
