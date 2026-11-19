"""issue #554: harness v0.20-0.22 机制接线 (destructive_gate / artifact_scan / json_repair)。

三个机制此前在 harness 内核可用但 cowork 零接线 (adoption-audit-2026-11-19):
- destructive_gate: ApprovalGate 分类 + 单次兑换 token; SANDBOX 层无 runner = fail-closed
- artifact_scan: 记忆/skill 产物进 run 前注入扫描 (prompt-injection/凭据泄漏规则)
- json_repair: LLM 输出 JSON 修复梯 (deterministic ladder), 修复计数进 run 遥测

本模块只做薄适配 — 决策语义全在内核, consumer wiring (审批卡/HITL park/遥测) 留 cowork。
"""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from ._harness_guard import harness_import_error

logger = logging.getLogger(__name__)

try:  # issue #554: 内核三机制 — harness 是强制安装 ([plugins] extra)
    from fusion_harness import (
        ApprovalGate,
        ArtifactQuarantine,
        ExecutionTier,
        JSONRepairError,
        SandboxExecRunner,
        SandboxPolicy,
        parse_repair,
        scan_artifact,
    )
except ImportError as exc:
    raise harness_import_error(exc) from exc

# 修复计数 — engine 在节点执行前 reset, 节点内的 parse_llm_json 递增;
# 计数随 node_finished 遥测上报 (issue #554: repairs in run telemetry)
json_repairs_in_step: ContextVar = ContextVar("json_repairs_in_step", default=0)


class KernelPermissionBridge:
    # ApprovalGate.permission_engine 协议: evaluate(tool, args) → 有 .decision 的对象;
    # cowork 语义: 显式 deny 规则命中 = deny, 其余交还人工闸门 (非 deny = 不拦)
    def __init__(self, permission_manager: Any = None):
        self._pm = permission_manager

    def evaluate(self, tool_name: str, args: Any = None) -> Any:
        from types import SimpleNamespace

        if self._pm is None:
            return SimpleNamespace(decision="allow")
        params = args if isinstance(args, dict) else {}
        with self._pm._lock:
            rules = list(self._pm.rules)
        for rule in rules:
            if rule.matches(tool_name, params) and not rule.allowed:
                return SimpleNamespace(decision="deny")
        return SimpleNamespace(decision="allow")


def build_approval_gate(human_gate: Any, permission_manager: Any = None) -> ApprovalGate:
    # issue #554: host 侧审批闸门 — 问题落共享 HumanGate 队列 (#527 桥), deny 规则前置
    return ApprovalGate(human_gate=human_gate, permission_engine=KernelPermissionBridge(permission_manager))


def classify_destructive(tool_name: str, args: Any = None) -> Tuple[bool, str]:
    # 内核分类表 + 名称 token (delete/drop/purge/...) — cowork 节点名直接适用
    return ApprovalGate.classify(tool_name, args)


def build_sandbox_policy() -> SandboxPolicy:
    # SANDBOX 层映射: FUSION_COWORK_SANDBOX_TOOLS (逗号分隔节点名) → SANDBOX tier;
    # FUSION_COWORK_SANDBOX_RUNNER=1 时接参考 runner (macOS sandbox-exec)。
    # 无 runner + SANDBOX tier → SandboxPolicy.decide 返回 BLOCKED (fail-closed)
    raw = os.environ.get("FUSION_COWORK_SANDBOX_TOOLS", "").strip()
    tiers: Dict[str, ExecutionTier] = {}
    if raw:
        for name in raw.split(","):
            name = name.strip()
            if name:
                tiers[name] = ExecutionTier.SANDBOX
    runner = None
    if os.environ.get("FUSION_COWORK_SANDBOX_RUNNER", "").strip().lower() in ("1", "true", "yes", "on"):
        runner = SandboxExecRunner()
    if tiers:
        logger.info(f"sandbox policy wired: tiers={sorted(tiers)} runner={'yes' if runner else 'none (fail-closed)'}")
    return SandboxPolicy(runner=runner, tool_tiers=tiers)


def sandbox_block_reason(policy: SandboxPolicy, tool_name: str, params: Dict[str, Any]) -> str:
    # SANDBOX tier + 无 runner = BLOCKED (fail-closed, 审批也救不回); 其余放行 ""
    try:
        from fusion_harness import permissions as _perm

        risk = _perm.score_tool_risk(tool_name, params)
    except Exception:
        risk = (0.0, "")
    decision = policy.decide(tool_name, params, risk)
    if decision.tier is ExecutionTier.BLOCKED:
        return decision.reason or "sandbox tier required but no runner wired"
    return ""


def scan_or_quarantine(
    text: str,
    source: str,
    kind: str,
    quarantine: Optional[ArtifactQuarantine] = None,
) -> Tuple[bool, Any]:
    # issue #554: 载入前扫描 — 返回 (clean, ScanReport); unclean 且给了隔离区则入隔离
    report = scan_artifact(text, source=source, kind=kind)
    if not report.clean and quarantine is not None:
        try:
            entry = quarantine.add(report, preview=text[:200])
            logger.warning(f"artifact quarantined: id={entry.get('id')} source={source} kind={kind}")
        except Exception as e:
            logger.warning(f"artifact quarantine write failed (scan result stands): {e}")
    return report.clean, report


def parse_llm_json(text: str, context: str = "") -> Any:
    # issue #554: LLM 输出 JSON 修复梯 — 严格解析失败走内核 ladder (围栏/尾逗号/
    # 截断闭合), 全败抛 JSONRepairError (typed error, 绝不静默返错数据)
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            pass
    value = parse_repair(text)
    json_repairs_in_step.set(json_repairs_in_step.get() + 1)
    logger.info(f"json repair ladder applied ({context or 'unknown'}): input={str(text)[:120]!r}")
    return value


__all__ = [
    "JSONRepairError",
    "KernelPermissionBridge",
    "build_approval_gate",
    "build_sandbox_policy",
    "classify_destructive",
    "json_repairs_in_step",
    "parse_llm_json",
    "sandbox_block_reason",
    "scan_or_quarantine",
]
