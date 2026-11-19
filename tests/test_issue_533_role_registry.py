"""#533 K4 Phase B item 2 — role_registry member-spec single representation.

Ruling: BusinessRole COMPOSES a kernel TeamMember; role_id/executor/duties
are derived properties of that member (no parallel fields). Registry stays
authoritative for cowork-only acceptor/deliverables/display_name, which
kernel TeamMember does not model.
"""

import pytest

from fusion_cowork.orchestrator.role_registry import (
    BusinessRole,
    RoleRegistry,
    get_role_registry,
    seed_default_roles,
    to_team_member,
)

try:
    from fusion_harness.team import TeamMember
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"fusion-harness missing: {exc}", allow_module_level=True)


class TestSingleRepresentation:
    def test_member_spec_is_the_composed_kernel_member(self):
        role = BusinessRole("shell_operator", "命令行执行", "executor_shell", duties=["run cmd"])
        assert isinstance(role.member, TeamMember)
        # derived properties read the SAME object — no dual representation
        assert role.role_id is role.member.name
        assert role.executor is role.member.role
        assert role.duties is role.member.skills

    def test_duties_mutation_is_visible_both_ways(self):
        role = BusinessRole("x", "x", "executor_node", duties=["a"])
        role.duties.append("b")
        assert role.member.skills == ["a", "b"]

    def test_to_team_member_returns_composed_member(self):
        role = BusinessRole("y", "y", "executor_mlx", duties=["s"])
        member = to_team_member(role)
        assert member is role.member

    def test_prebuilt_member_accepted(self):
        member = TeamMember(name="pre", role="executor_shell", skills=["k"])
        role = BusinessRole("ignored", "dn", "executor_shell", member=member)
        assert role.role_id == "pre"
        assert role.executor == "executor_shell"


class TestRegistryAuthorityUnchanged:
    def test_acceptor_deliverables_stay_registry_authoritative(self):
        reg = RoleRegistry()
        seed_default_roles(reg)
        planner = reg.get("task_planner")
        # kernel TeamMember has no acceptor/deliverables — cowork-only fields
        assert not hasattr(planner.member, "acceptor")
        assert not hasattr(planner.member, "deliverables")
        assert planner.acceptor == "coordinator"
        assert planner.deliverables == "结构化子任务清单 (JSON)"

    def test_export_definition_still_valid(self):
        from fusion_harness.team import TeamDefinition

        definition = get_role_registry().to_team_definition("cowork-orchestrator", "cowork.orchestrator")
        assert isinstance(definition, TeamDefinition)
        assert len(definition.members) == 6

    def test_executor_validation_unchanged(self):
        reg = RoleRegistry()
        with pytest.raises(ValueError):
            reg.register(BusinessRole("bad", "bad", "executor_unknown"))
