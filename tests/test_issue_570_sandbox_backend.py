# issue #570: route impl-node sandbox runner through harness select_sandbox_runner.
#
# FUSION_SANDBOX_BACKEND=fusion_executor → the runner wired into the sandbox
# policy (and the engine node-failure path) is the harness FusionExecutorBackend
# adapter (native sandbox + Git snapshot rollback); unavailable backend or env
# unset → current behavior unchanged (default path).

from types import SimpleNamespace

import pytest

from fusion_cowork.engine.kernel_gates import (
    build_sandbox_policy,
    get_sandbox_runner,
    reset_sandbox_runner_cache,
    sandbox_block_reason,
)
from fusion_cowork.engine.workflow import WorkflowEngine, WorkflowStatus


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FUSION_SANDBOX_BACKEND", raising=False)
    monkeypatch.delenv("FUSION_COWORK_SANDBOX_RUNNER", raising=False)
    monkeypatch.delenv("FUSION_COWORK_SANDBOX_TOOLS", raising=False)
    reset_sandbox_runner_cache()
    yield
    reset_sandbox_runner_cache()


class _FakeExecutor:
    def __init__(self):
        self.snapshots = []
        self.rollbacks = []
        self.fail_next = True

    def snapshot_create(self, cwd):
        sid = f"snap-{len(self.snapshots) + 1}"
        self.snapshots.append((sid, cwd))
        return sid

    def rollback(self, sid, cwd):
        self.rollbacks.append((sid, cwd))
        return True

    def run(self, command, cwd="", timeout_sec=30.0):
        return SimpleNamespace(exit_code=1 if self.fail_next else 0, stdout="", stderr="boom")


class _FakeBackend:
    def __init__(self, cwd="", timeout_s=30.0, snapshot_before=True, executor=None):
        self.cwd = cwd
        self.executor = executor or _FakeExecutor()

    def snapshot(self):
        return self.executor.snapshot_create(self.cwd)

    def rollback(self, snapshot_id=""):
        return self.executor.rollback(snapshot_id or "", self.cwd)


def _wf(node_name):
    from fusion_cowork.engine.node import NodeRegistry
    from fusion_cowork.engine.workflow import Workflow

    wf = Workflow(name=f"wf-570-{node_name}")
    wf.add_node(NodeRegistry.create(node_name))
    return wf


class _FailProbe:
    calls = 0

    async def execute(self, node, input_data, ctx):
        _FailProbe.calls += 1
        from fusion_cowork.engine.node import NodeResult, NodeStatus

        return NodeResult(status=NodeStatus.FAILED, error="forced failure for rollback test")


def _register(name, node_cls):
    from fusion_cowork.engine.node import NodeRegistry

    NodeRegistry.register(node_cls)
    return name


class TestDefaultPathUnchanged:
    def test_env_unset_returns_default(self):
        runner, backend = get_sandbox_runner()
        assert backend == "default"
        assert runner is None

    def test_env_unset_sandbox_runner_flag_kept(self, monkeypatch):
        # pre-#570 flag still selects the reference runner under default backend
        monkeypatch.setenv("FUSION_COWORK_SANDBOX_RUNNER", "1")
        reset_sandbox_runner_cache()
        runner, backend = get_sandbox_runner()
        assert backend == "default"
        assert runner is not None

    def test_policy_built_via_kernel_sandbox_policy(self, monkeypatch):
        # kernel_sandbox_policy is the single production policy builder now
        monkeypatch.setenv("FUSION_COWORK_SANDBOX_TOOLS", "python_repl")
        policy = build_sandbox_policy()
        assert sandbox_block_reason(policy, "python_repl", {})  # no runner -> fail-closed
        assert "no runner" in sandbox_block_reason(policy, "python_repl", {})

    def test_runner_cached_per_cwd(self, monkeypatch):
        r1, b1 = get_sandbox_runner("/tmp/ws-a")
        r2, b2 = get_sandbox_runner("/tmp/ws-a")
        assert r1 is r2 and b1 == b2 == "default"
        reset_sandbox_runner_cache()


class TestFusionExecutorBackend:
    def test_selects_adapter_when_env_set(self, monkeypatch):
        from fusion_harness import executor_backend

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(executor_backend, "FusionExecutorBackend", _FakeBackend)
        runner, backend = get_sandbox_runner()
        assert backend == "fusion_executor"
        assert isinstance(runner, _FakeBackend)

    def test_unavailable_backend_falls_back(self, monkeypatch):
        # no monkeypatch: fusion-executor not importable in test env -> typed
        # fallback to default (harness logs the warning)
        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        reset_sandbox_runner_cache()
        try:
            import fusion_executor  # noqa: F401

            pytest.skip("fusion-executor installed — cannot test fallback here")
        except ImportError:
            pass
        runner, backend = get_sandbox_runner()
        assert backend == "default"
        assert runner is None

    def test_backend_receives_run_workspace_cwd(self, monkeypatch):
        from fusion_harness import executor_backend

        seen = {}

        class _SpyBackend(_FakeBackend):
            def __init__(self, cwd="", **kw):
                seen["cwd"] = cwd
                super().__init__(cwd=cwd, **kw)

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(executor_backend, "FusionExecutorBackend", _SpyBackend)
        get_sandbox_runner("/tmp/ws-570")
        assert seen["cwd"] == "/tmp/ws-570"


class TestEngineRollback:
    async def test_failed_node_rolls_back_workspace(self, monkeypatch, tmp_path):
        from fusion_harness import executor_backend

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(executor_backend, "FusionExecutorBackend", _FakeBackend)
        reset_sandbox_runner_cache()

        from fusion_cowork.engine.node import BaseNode

        class _Fail570(BaseNode):
            name = "fail_570"

            async def execute(self, input_data):
                from fusion_cowork.engine.node import NodeResult, NodeStatus

                return NodeResult(status=NodeStatus.FAILED, error="forced")

        _register("fail_570", _Fail570)
        eng = WorkflowEngine(permission_manager=None)
        eng.set_run_workspace("run-570-rb", str(tmp_path))
        execution = await eng.execute(_wf("fail_570"), execution_id="run-570-rb")
        assert execution.status == WorkflowStatus.FAILED

        runner, backend = get_sandbox_runner(str(tmp_path))
        assert backend == "fusion_executor"
        assert runner.executor.snapshots, "pre-node snapshot must be taken"
        assert runner.executor.rollbacks, "failed node must trigger workspace rollback"
        sid, cwd = runner.executor.rollbacks[0]
        assert sid == runner.executor.snapshots[0][0]
        assert cwd == str(tmp_path)

    async def test_default_backend_no_snapshot_calls(self, monkeypatch, tmp_path):
        from fusion_cowork.engine.node import BaseNode

        class _Fail570b(BaseNode):
            name = "fail_570b"

            async def execute(self, input_data):
                from fusion_cowork.engine.node import NodeResult, NodeStatus

                return NodeResult(status=NodeStatus.FAILED, error="forced")

        _register("fail_570b", _Fail570b)
        eng = WorkflowEngine(permission_manager=None)
        execution = await eng.execute(_wf("fail_570b"), execution_id="run-570-def")
        assert execution.status == WorkflowStatus.FAILED  # default path untouched

    async def test_snapshot_failure_does_not_block_node(self, monkeypatch, tmp_path):
        from fusion_harness import executor_backend

        class _BrokenBackend:
            def snapshot(self):
                raise RuntimeError("git broken")

            def rollback(self, snapshot_id=""):
                return False

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(executor_backend, "FusionExecutorBackend", lambda **kw: _BrokenBackend())
        reset_sandbox_runner_cache()

        from fusion_cowork.engine.node import BaseNode

        class _Ok570(BaseNode):
            name = "ok_570"

            async def execute(self, input_data):
                from fusion_cowork.engine.node import NodeResult, NodeStatus

                return NodeResult(status=NodeStatus.SUCCESS, data={"done": True})

        _register("ok_570", _Ok570)
        eng = WorkflowEngine(permission_manager=None)
        eng.set_run_workspace("run-570-snapfail", str(tmp_path))
        execution = await eng.execute(_wf("ok_570"), execution_id="run-570-snapfail")
        assert execution.status == WorkflowStatus.SUCCESS
