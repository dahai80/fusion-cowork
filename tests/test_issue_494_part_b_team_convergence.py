# issue #494 Part B: orchestrator convergence onto kernel TeamRuntime.
#
# Deliverables here are the kernel-typed export boundary + persisted-state
# compat proof. The durable mailbox table is the cross-repo wire protocol
# with agent-studio (shared FUSION_TEAM_DB) — swapping storage to kernel
# TeamRuntime would break the other daemon's consumer, so TeamMailbox stays
# the protocol owner and exports rows as kernel TeamMessage instead (see
# team_mailbox.py / runner_pool.py convergence notes).

import pytest

from fusion_cowork.orchestrator.role_registry import (
    BusinessRole,
    RoleRegistry,
    get_role_registry,
    seed_default_roles,
    to_team_member,
)
from fusion_cowork.orchestrator.team_mailbox import CoworkRoster, TeamMailbox, row_to_team_message

try:
    from fusion_harness.team import TeamDefinition, TeamMember
    from fusion_harness.team_runtime import TeamMessage
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"fusion-harness missing: {exc}", allow_module_level=True)


class TestKernelExport:
    def test_business_role_to_team_member(self):
        # i18n: user-facing
        role = BusinessRole("shell_operator", "命令行执行", "executor_shell", duties=["run cmd"], deliverables="stdout")
        member = to_team_member(role)
        assert isinstance(member, TeamMember)
        assert member.name == "shell_operator"
        assert member.role == "executor_shell"
        assert member.skills == ["run cmd"]

    def test_default_registry_exports_team_definition(self):
        reg = get_role_registry()
        definition = reg.to_team_definition("cowork-orchestrator", "cowork.orchestrator")
        assert isinstance(definition, TeamDefinition)
        assert definition.namespace_key == "cowork.orchestrator"
        assert len(definition.members) == 6  # seed_default_roles baseline
        names = {m.name for m in definition.members}
        assert {
            "task_planner",
            "coordinator",
            "shell_operator",
            "node_operator",
            "workflow_builder",
            "result_reviewer",
        } == names

    def test_fresh_registry_roundtrip(self):
        reg = RoleRegistry()
        seed_default_roles(reg)
        definition = reg.to_team_definition("fresh", "test.ns")
        assert isinstance(definition, TeamDefinition)
        assert len(definition.members) == 6

    def test_registry_validation_unchanged(self):
        reg = RoleRegistry()
        with pytest.raises(ValueError):
            reg.register(BusinessRole("bad", "bad", "executor_unknown"))


class TestMailboxExport:
    def test_row_to_team_message_roundtrip(self, tmp_path):
        mailbox = TeamMailbox(str(tmp_path / "team.db"))
        sent = mailbox.send("alice", "bob", body="hello", kind="message", team="t1")
        rows = mailbox.inbox("bob")
        assert len(rows) == 1
        msg = row_to_team_message(rows[0])
        assert isinstance(msg, TeamMessage)
        assert msg.msg_id == sent["message_id"]
        assert msg.sender == "alice"
        assert msg.recipient == "bob"
        assert msg.body == "hello"
        # cowork kind "message" not in kernel MSG_TYPES → "text" fallback
        assert msg.type == "text"

    def test_kernel_type_passes_through(self, tmp_path):
        mailbox = TeamMailbox(str(tmp_path / "team.db"))
        mailbox.send("alice", "bob", body="plan", kind="text")
        msg = row_to_team_message(mailbox.inbox("bob")[0])
        assert msg.type == "text"


class TestPersistedStateCompat:
    def test_mailbox_state_reloadable(self, tmp_path):
        # current-format state files stay loadable: writer instance → rows on
        # disk → fresh reader instance sees them (compat with existing DBs)
        path = str(tmp_path / "team.db")
        writer = TeamMailbox(path)
        writer.send("a", "b", body="m1")
        writer.send("c", "b", body="m2", kind="reply")
        reader = TeamMailbox(path)
        unread = reader.inbox("b")
        assert [r["body"] for r in unread] == ["m1", "m2"]
        assert reader.depth("b") == 2
        assert reader.depth() == 2
        assert reader.ack("b", unread[0]["message_id"]) is True
        assert reader.depth("b") == 1

    def test_roster_state_reloadable(self, tmp_path):
        path = str(tmp_path / "team.db")
        writer = CoworkRoster(path)
        writer.register("agent-1", "wf-1", name="Alpha", team="ops")
        reader = CoworkRoster(path)
        row = reader.get("agent-1")
        assert row["workflow_id"] == "wf-1"
        assert row["enabled"] == 1
        assert len(reader.list(enabled_only=True)) == 1
        assert reader.remove("agent-1") is True
        assert reader.get("agent-1") is None
