"""#533 K4 Phase B item 1 — comm.py MessageBus classification pin.

Ruling: kernel-verified DIVERGENT-BY-DESIGN (see fusion_cowork/orchestrator/comm.py
module header). The kernel TeamRuntime mailbox is member-scoped with a closed
MSG_TYPES set and durable file persistence; the cowork bus is an open in-process
pub/sub transport for DAG orchestrator internals. This test pins both sides of
the divergence so a silent drift on either side fails loudly and forces the
classification to be revisited.
"""

import pytest

from fusion_cowork.orchestrator.comm import (
    AgentMessageBus,
    MessageDeliveryError,
)


@pytest.fixture
def bus():
    return AgentMessageBus()


class TestKernelContractPins:
    def test_kernel_msg_types_still_closed_set(self):
        from fusion_harness.team_runtime import MSG_TYPES

        # harness 0.29.0 added destructive_gate (ApprovalGate operator-inbox
        # kind, consumed via #554); the set stays closed — pin the new contract
        assert MSG_TYPES == (
            "text",
            "plan_approval",
            "shutdown_request",
            "shutdown_response",
            "destructive_gate",
        )

    def test_kernel_send_requires_members(self):
        from fusion_harness.team_runtime import TeamMessage, TeamRuntime

        from fusion_cowork.orchestrator.comm import AgentMessage

        assert AgentMessage is not None  # sibling transport exists by design

        runtime = TeamRuntime.__new__(TeamRuntime)
        # no definition wired — member validation must reject unknown senders
        with pytest.raises(AttributeError):
            runtime.send(TeamMessage(type="text", sender="ghost", recipient="ghost2", body="x"))


class TestCoworkBusContractPins:
    async def test_open_topic_arbitrary_payload(self, bus):
        q = bus.subscribe("any:topic:string")
        msg_id = await bus.publish("any:topic:string", "system-component", {"nested": {"data": 1}})
        assert (await q.get()).msg_id == msg_id

    async def test_broadcast_receiver_star(self, bus):
        # cowork "*" broadcast fans out to the literal topic "inbox:*"
        q1 = bus.subscribe("inbox:*")
        q2 = bus.subscribe("inbox:*")
        await bus.send("orchestrator", "*", {"kind": "halt"})
        assert (await q1.get()).payload["kind"] == "halt"
        assert (await q2.get()).payload["kind"] == "halt"

    async def test_e6_delivery_error_on_full_queues(self, bus):
        q = bus.subscribe("inbox:slow")
        # saturate the single subscriber queue (maxsize _QUEUE_MAX)
        from fusion_cowork.orchestrator.comm import _QUEUE_MAX

        for _ in range(_QUEUE_MAX):
            q.put_nowait(object())
        with pytest.raises(MessageDeliveryError):
            await bus.send("a", "slow", {"x": 1})
        assert len(bus.get_dropped()) == 1
