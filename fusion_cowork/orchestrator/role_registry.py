"""Business role registry (audit v2 方案五).

Upgrades "roles" from executor aliases to business role definitions:
duties / deliverables / default acceptor / executor mapping. The planner
prompt, the schema validator whitelist, and acceptance auto-bind all read
from this single source — previously the executor enum was duplicated in
three places (prompt text, validator, role_map) with silent-drift risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The physical executor ids (execution layer, unchanged by business roles)
EXECUTOR_ROLES = ("executor_node", "executor_workflow", "executor_mlx", "executor_shell")


@dataclass
class BusinessRole:
    """A business role: what it does, what it delivers, who accepts."""

    role_id: str
    display_name: str
    executor: str  # one of EXECUTOR_ROLES
    duties: list = field(default_factory=list)
    deliverables: str = ""
    acceptor: str = ""  # default acceptor for accountability auto-bind


class RoleRegistry:
    """Registry of business roles; single source for planner + validation."""

    def __init__(self) -> None:
        self._roles: dict = {}

    def register(self, role: BusinessRole) -> None:
        if role.executor not in EXECUTOR_ROLES:
            raise ValueError(f"role {role.role_id}: unknown executor '{role.executor}'")
        self._roles[role.role_id] = role

    def get(self, role_id: str):
        return self._roles.get(role_id)

    def role_ids(self) -> list:
        return sorted(self._roles)

    def executor_id_set(self) -> set:
        return set(EXECUTOR_ROLES)

    def executor_for(self, role_id: str, default: str = "executor_node") -> str:
        role = self._roles.get(role_id)
        return role.executor if role else default

    def prompt_catalog(self) -> str:
        """Planner-facing catalog: role id, executor, acceptor, duties, deliverables."""
        lines = []
        for r in sorted(self._roles.values(), key=lambda x: x.role_id):
            duties = "; ".join(r.duties) if r.duties else r.display_name
            acc = f", 默认验收人={r.acceptor}" if r.acceptor else ""
            lines.append(f"- {r.role_id} (executor={r.executor}{acc}): {duties}; 交付物: {r.deliverables}")
        return "\n".join(lines)


def seed_default_roles(registry: RoleRegistry) -> None:
    """Built-in business roles covering the standard pipeline."""
    defaults = [
        BusinessRole(
            "task_planner",
            "任务规划",
            "executor_mlx",
            duties=["理解业务需求", "拆解为可执行子任务", "识别依赖与验收标准"],
            deliverables="结构化子任务清单 (JSON)",
            acceptor="coordinator",
        ),
        BusinessRole(
            "coordinator",
            "协调者",
            "executor_mlx",
            duties=["接收拆解结果", "分派执行", "汇总进展并对最终结果兜底"],
            deliverables="执行编排与进展说明",
            acceptor="",
        ),
        BusinessRole(
            "shell_operator",
            "命令行执行",
            "executor_shell",
            duties=["按要求执行 shell 命令", "回报真实输出与退出码"],
            deliverables="命令输出 (stdout/stderr/returncode)",
            acceptor="coordinator",
        ),
        BusinessRole(
            "node_operator",
            "节点执行",
            "executor_node",
            duties=["按 catalog 执行指定工作流节点", "回报节点结果数据"],
            deliverables="节点结果数据",
            acceptor="coordinator",
        ),
        BusinessRole(
            "workflow_builder",
            "工作流执行",
            "executor_workflow",
            duties=["构建/执行完整工作流或模板"],
            deliverables="工作流执行结果",
            acceptor="coordinator",
        ),
        BusinessRole(
            "result_reviewer",
            "结果分析",
            "executor_mlx",
            duties=["汇总各子任务结果", "对照验收标准质检", "给出结论与风险提示"],
            deliverables="分析结论与风险提示",
            acceptor="",
        ),
    ]
    for r in defaults:
        registry.register(r)


_DEFAULT_REGISTRY = RoleRegistry()
seed_default_roles(_DEFAULT_REGISTRY)


def get_role_registry() -> RoleRegistry:
    return _DEFAULT_REGISTRY
