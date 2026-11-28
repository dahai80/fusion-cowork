# issue #653: multi-repo orchestration — spec-first runtime behind
# FUSION_COWORK_MULTI_REPO=1. Synthetic 2-repo run (repo B waits on repo A
# artifact; A failure cancels B cleanly), admission validation, credential
# rejection, timings aggregation, flag-off zero behavior.

import asyncio

import pytest

from fusion_cowork.engine import multi_repo as mr
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine


class MockSuccessNode(BaseNode):
    name = "mock_success"
    display_name = "Mock success node"
    category = NodeCategory.TOOL
    description = "test: always succeeds"
    icon = "T"
    default_label = "Mock success"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": "ok"}, summary="ok")


class ArtifactNode(MockSuccessNode):
    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"contract": "sha-650"}, summary="artifact")


class MockFailNode(BaseNode):
    name = "mock_fail"
    display_name = "Mock fail node"
    category = NodeCategory.TOOL
    description = "test: always fails"
    icon = "T"
    default_label = "Mock fail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="repo A build broke")


def _repo_params(url: str, **extra):
    params = {"repo": {"url": url, "ref": "main"}}
    params.update(extra)
    return params


def _two_repo_workflow(a_node, b_params):
    wf = Workflow(name="wf653")
    a = a_node(node_id="a")
    a.config.params = _repo_params("git@github.com:acme/client.git")
    b = MockSuccessNode(node_id="b")
    b.config.params = b_params
    wf.add_node(a)
    wf.add_node(b)
    wf.connect("a", "b")
    return wf


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(mr.MULTI_REPO_ENV, "1")


def _run(engine, wf):
    return asyncio.run(engine.execute(wf))


class TestSyntheticTwoRepo:
    def test_b_waits_on_a_artifact(self, flag_on):
        wf = _two_repo_workflow(
            ArtifactNode, _repo_params("git@github.com:acme/service.git", waits_on_artifact="a:contract")
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        execution = _run(eng, wf)
        assert execution.status.value == "success"
        b_step = next(s for s in execution.steps if s.node_id == "b")
        a_step = next(s for s in execution.steps if s.node_id == "a")
        assert b_step.started_at >= a_step.completed_at
        # per-repo timings aggregate both stages
        specs = mr.collect_repo_specs(wf)
        timings = mr.repo_timings(specs, execution.steps)
        assert set(timings) == {"git@github.com:acme/client.git", "git@github.com:acme/service.git"}
        assert timings["git@github.com:acme/client.git"]["total_s"] >= 0

    def test_a_failure_cancels_b(self, flag_on):
        wf = _two_repo_workflow(
            MockFailNode, _repo_params("git@github.com:acme/service.git", waits_on_artifact="a:contract")
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        execution = _run(eng, wf)
        assert execution.status.value == "failed"
        b_steps = [s for s in execution.steps if s.node_id == "b"]
        # repo B never executed — clean cancel, no partial run
        assert all(s.status != NodeStatus.SUCCESS for s in b_steps)
        assert not any(getattr(s, "execution_time", 0) and s.status == NodeStatus.SUCCESS for s in b_steps)

    def test_waits_on_artifact_requires_edge(self, flag_on):
        wf = _two_repo_workflow(
            ArtifactNode, _repo_params("git@github.com:acme/service.git", waits_on_artifact="ghost:contract")
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        with pytest.raises(ValueError, match="unknown node 'ghost'"):
            _run(eng, wf)


class TestCredentialRejection:
    def test_credential_literal_rejected(self, flag_on):
        wf = Workflow(name="wf653c")
        a = MockSuccessNode(node_id="a")
        a.config.params = {"repo": {"url": "git@github.com:acme/client.git", "token": "ghp_literal"}}
        wf.add_node(a)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        with pytest.raises(ValueError, match="inline credential"):
            _run(eng, wf)

    def test_nested_credential_rejected(self, flag_on):
        wf = Workflow(name="wf653c2")
        a = MockSuccessNode(node_id="a")
        a.config.params = {"repo": {"url": "git@github.com:acme/client.git", "auth": {"ssh_key": "MITM"}}}
        wf.add_node(a)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        with pytest.raises(ValueError, match="inline credential"):
            _run(eng, wf)


class TestFlagOff:
    def test_flag_off_ignores_repo_params(self, monkeypatch):
        # even a credential-literal workflow runs unchanged with the flag off —
        # existing workflows never regress
        monkeypatch.delenv(mr.MULTI_REPO_ENV, raising=False)
        wf = _two_repo_workflow(
            ArtifactNode, {"repo": {"url": "git@github.com:acme/service.git", "token": "ghp_literal"}}
        )
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        execution = _run(eng, wf)
        assert execution.status.value == "success"


class TestResolverAndHelpers:
    def test_resolve_rejects_non_identity_uri(self):
        with pytest.raises(ValueError, match="identity://"):
            mr.resolve_repo_token("env:SOME_TOKEN")

    def test_resolve_requires_identity_url(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_URL", raising=False)
        with pytest.raises(ValueError, match="FUSION_IDENTITY_URL"):
            mr.resolve_repo_token("identity://acme/repo/push")

    def test_repo_workspace_hash_stable(self, tmp_path):
        w1 = mr.repo_workspace("git@x:a.git", root=tmp_path)
        w2 = mr.repo_workspace("git@x:a.git", root=tmp_path)
        assert w1 == w2 and w1.is_dir()

    def test_empty_repo_url_rejected(self, flag_on):
        wf = Workflow(name="wf653e")
        a = MockSuccessNode(node_id="a")
        a.config.params = {"repo": {"ref": "main"}}
        wf.add_node(a)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=False)
        with pytest.raises(ValueError, match="params.repo.url is required"):
            _run(eng, wf)
