"""#533 K4 Phase B item 3 — runner_pool run_member divergence re-ratified.

Ruling (see fusion_cowork/orchestrator/runner_pool.py header): kernel
TeamRuntime.run_member is LLM-subagent execution (SubagentRunner mechanism,
adopted in the space plane #496 and teams.run_member node); RunnerPool
execution is workflow dispatch through the cowork WorkflowEngine with
durable mailbox semantics (at-least-once ack, in-flight dedup). The
divergence is the ratified end state, not a pending gap. This test pins
both mechanisms so silent drift on either side forces the ruling to be
revisited.
"""

import inspect


class TestKernelMechanismPins:
    def test_kernel_run_member_is_subagent_execution(self):
        from fusion_harness.team_runtime import TeamRuntime

        src = inspect.getsource(TeamRuntime.run_member)
        # mechanism signature: SpawnBrief contract + SubagentRunner, no
        # workflow-dispatch parameter exists
        assert "SpawnBrief" in src
        assert "SubagentSpec" in src or "SubagentRunner" in src
        assert "depth" in src

    def test_kernel_run_member_takes_no_workflow_dispatch(self):
        from fusion_harness.team_runtime import TeamRuntime

        sig = inspect.signature(TeamRuntime.run_member)
        assert "dispatch" not in sig.parameters


class TestPoolContractPins:
    async def test_pool_executes_via_dispatch_callback(self):
        from fusion_cowork.orchestrator.runner_pool import RunnerPool

        dispatched = []

        async def dispatch(agent, message):
            dispatched.append((agent["agent_id"], message["message_id"]))
            return "run_1"

        pool = RunnerPool(roster=_FakeRoster(), mailbox=_FakeMailbox(), dispatch=dispatch, reply=False)
        await pool._dispatch_one({"agent_id": "agent_a"}, {"message_id": "m1", "sender": "bob"})
        assert dispatched == [("agent_a", "m1")]
        assert pool._in_flight == {"m1": "run_1"}
        assert pool.metrics["consumed"] == 1

    async def test_dispatch_failure_does_not_ack(self):
        from fusion_cowork.orchestrator.runner_pool import RunnerPool

        acked = []

        async def dispatch(agent, message):
            raise RuntimeError("workflow launch failed")

        mailbox = _FakeMailbox(on_ack=acked.append)
        pool = RunnerPool(roster=_FakeRoster(), mailbox=mailbox, dispatch=dispatch, reply=False)
        await pool._dispatch_one({"agent_id": "agent_a"}, {"message_id": "m1", "sender": "bob"})
        assert not acked, "failed dispatch must not ack (at-least-once)"
        assert pool._in_flight == {}
        assert pool.metrics["dispatch_errors"] == 1

    async def test_in_flight_dedup_skips_duplicate_dispatch(self):
        from fusion_cowork.orchestrator.runner_pool import RunnerPool

        calls = []

        async def dispatch(agent, message):
            calls.append(message["message_id"])
            return "run_1"

        pool = RunnerPool(roster=_FakeRoster(), mailbox=_FakeMailbox(), dispatch=dispatch, reply=False)
        pool._in_flight["m1"] = "run_pending"
        await pool._dispatch_one({"agent_id": "agent_a"}, {"message_id": "m1", "sender": "bob"})
        assert not calls, "duplicate message must not re-dispatch"


class _FakeRoster:
    def list(self, enabled_only=True):
        return [{"agent_id": "agent_a", "enabled": True}]


class _FakeMailbox:
    def __init__(self, on_ack=None):
        self._on_ack = on_ack

    def inbox(self, agent_id):
        return []

    def ack(self, agent_id, message_id):
        if self._on_ack:
            self._on_ack(message_id)
        return True
