"""issue #554: harness v0.20-0.22 mechanism wiring (destructive_gate / artifact_scan / json_repair).

All three mechanisms were available in the harness kernel but had zero cowork wiring (adoption-audit-2026-11-19):
- destructive_gate: ApprovalGate classification + single-redeem token; SANDBOX tier without a runner = fail-closed
- artifact_scan: injection scan for memory/skill artifacts before they enter a run (prompt-injection/credential-leak rules)
- json_repair: LLM output JSON repair ladder (deterministic ladder), repair counts feed run telemetry

This module is a thin adapter only — decision semantics live entirely in the kernel; consumer wiring
(approval cards / HITL park / telemetry) stays in cowork.
"""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from ..security.kernel_sandbox import kernel_sandbox_policy
from ._harness_guard import harness_import_error

logger = logging.getLogger(__name__)

try:  # issue #554: the three kernel mechanisms — harness is a mandatory install ([plugins] extra)
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
    from fusion_harness.executor_backend import select_sandbox_runner
except ImportError as exc:
    raise harness_import_error(exc) from exc

# Repair counter — engine resets it before node execution, parse_llm_json inside the node increments it;
# the count is reported with node_finished telemetry (issue #554: repairs in run telemetry).
# The ContextVar holds a mutable list (asyncio.wait_for creates the task with a copied context — an int set
# never propagates back, while in-place += on a shared list is visible to the parent coroutine); unset =
# outside node context, incrementing is a no-op.
json_repairs_in_step: ContextVar = ContextVar("json_repairs_in_step")


class KernelPermissionBridge:
    # ApprovalGate.permission_engine protocol: evaluate(tool, args) → an object with .decision;
    # cowork semantics: an explicit deny-rule hit = deny, everything else goes back to the human gate (non-deny = not blocked)
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
    # issue #554: host-side approval gate — questions go to the shared HumanGate queue (#527 bridge), deny rules evaluated first
    return ApprovalGate(human_gate=human_gate, permission_engine=KernelPermissionBridge(permission_manager))


def classify_destructive(tool_name: str, args: Any = None) -> Tuple[bool, str]:
    # kernel classification table + name tokens (delete/drop/purge/...) — applies to cowork node names directly
    return ApprovalGate.classify(tool_name, args)


def build_sandbox_policy() -> SandboxPolicy:
    # SANDBOX tier mapping: FUSION_COWORK_SANDBOX_TOOLS (comma-separated node names) → SANDBOX tier;
    # with FUSION_COWORK_SANDBOX_RUNNER=1 the reference runner is wired (macOS sandbox-exec).
    # No runner + SANDBOX tier → SandboxPolicy.decide returns BLOCKED (fail-closed)
    raw = os.environ.get("FUSION_COWORK_SANDBOX_TOOLS", "").strip()
    tiers: Dict[str, ExecutionTier] = {}
    if raw:
        for name in raw.split(","):
            name = name.strip()
            if name:
                tiers[name] = ExecutionTier.SANDBOX
    runner, _backend = get_sandbox_runner()
    if tiers:
        logger.info(f"sandbox policy wired: tiers={sorted(tiers)} runner={'yes' if runner else 'none (fail-closed)'}")
    # risk_threshold=inf — the permission gate only honors explicit tier mappings (opt-in); the kernel's default
    # semantics of auto-promoting to SANDBOX by risk score would fail-closed block unconfigured high-risk tools
    # (shell_exec rm -rf etc.) before guard delegation, breaking #73's existing behavior
    # issue #570: built via kernel_sandbox_policy (the helper's only production call site, no parallel policy construction)
    return kernel_sandbox_policy(runner, tool_tiers=tiers, risk_threshold=float("inf"))


# issue #570: harness select_sandbox_runner wiring — when FUSION_SANDBOX_BACKEND=
# fusion_executor, impl nodes go through FusionExecutorBackend (native sandbox + Git snapshot
# rollback); unset/unavailable → the current runner is returned as-is (zero behavior change on the default path).
# Cache the singleton by cwd — the permission gate builds a policy on every check; without caching each
# tool call would create a new backend and snapshot state (last_snapshot_id) would overwrite each other.
_sandbox_runner_cache: Dict[str, Tuple[Any, str]] = {}


def get_sandbox_runner(cwd: str = "") -> Tuple[Any, str]:
    key = cwd or ""
    cached = _sandbox_runner_cache.get(key)
    if cached is not None:
        return cached
    runner = None
    if os.environ.get("FUSION_COWORK_SANDBOX_RUNNER", "").strip().lower() in ("1", "true", "yes", "on"):
        runner = SandboxExecRunner()
    preferred = os.environ.get("FUSION_SANDBOX_BACKEND", "default").strip() or "default"
    backend_kwargs: Optional[Dict[str, Any]] = {"cwd": cwd} if cwd else None
    runner, backend = select_sandbox_runner(preferred=preferred, default_runner=runner, backend_kwargs=backend_kwargs)
    if backend != "default":
        logger.info(f"sandbox runner via harness select_sandbox_runner: backend={backend} cwd={cwd or 'process'}")
    _sandbox_runner_cache[key] = (runner, backend)
    return runner, backend


def reset_sandbox_runner_cache() -> None:
    # Clear the cache after tests/config changes (changed env means the cached runner no longer matches)
    _sandbox_runner_cache.clear()


# ── issue #571: MAST failure attribution wiring (fusion_harness.failure_attribution) ──
# When a node fails, attribute the current run trajectory; the verdict (mode/cluster/confidence/evidence) lands in
# the node result + run summary; spec-class (specification_system_design) failures no longer burn retry
# budget (escalation semantics — attribution travels with the run summary to QA/rework routing). env default off = zero behavior change.
ATTRIBUTION_ENV = "FUSION_COWORK_FAILURE_ATTRIBUTION"
SPEC_CLUSTER = "specification_system_design"


def failure_attribution_enabled() -> bool:
    return os.environ.get(ATTRIBUTION_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def attribute_node_failure(
    exec_id: str,
    steps: Any,
    failing_node_id: str,
    error: str,
) -> Optional[Dict[str, Any]]:
    # MAST attribution — trajectory = completed steps (tool_result) + the failing node's error record;
    # analysis/classification semantics live entirely in the kernel FailureAnalyzer, this only adapts the record shape
    try:
        from fusion_harness.failure_attribution import FailureAnalyzer

        from ..trajectory.recorder import kernel_record
    except Exception as e:
        logger.warning(f"failure attribution unavailable (ignored): {e}")
        return None
    records = []
    for s in steps or []:
        records.append(
            kernel_record(
                "post_node_execute",
                exec_id,
                node_id=str(getattr(s, "node_id", "") or "workflow"),
                payload={
                    "summary": str(getattr(s, "summary", "") or ""),
                    "error": str(getattr(s, "error", "") or ""),
                },
                status=str(getattr(getattr(s, "status", None), "value", "") or ""),
            )
        )
    records.append(
        kernel_record(
            "node_error",
            exec_id,
            node_id=str(failing_node_id or "workflow"),
            payload={"error": str(error or "")},
            status="failed",
        )
    )
    try:
        report = FailureAnalyzer().analyze(records)
    except Exception as e:
        logger.warning(f"failure attribution analyze failed (ignored): {e}")
        return None
    run = (report.get("runs") or {}).get(exec_id) or {}
    verdicts = run.get("verdicts") or []
    return verdicts[0] if verdicts else None


def is_spec_failure(verdict: Optional[Dict[str, Any]]) -> bool:
    return bool(verdict) and verdict.get("cluster") == SPEC_CLUSTER


def sandbox_block_reason(policy: SandboxPolicy, tool_name: str, params: Dict[str, Any]) -> str:
    # SANDBOX tier + no runner = BLOCKED (fail-closed, approval cannot rescue it); everything else allows ""
    # risk score/band not involved — the permission gate only honors explicit tier mappings (opt-in); the kernel's default
    # semantics of auto-promoting to SANDBOX by risk would fail-closed block unconfigured high-risk tools
    # (shell_exec rm -rf etc.) before #73 guard delegation, breaking existing behavior
    decision = policy.decide(tool_name, params, (0.0, ""))
    if decision.tier is ExecutionTier.BLOCKED:
        return decision.reason or "sandbox tier required but no runner wired"
    return ""


_artifact_quarantine: Optional[ArtifactQuarantine] = None


def get_quarantine() -> ArtifactQuarantine:
    # Process-level quarantine singleton — artifacts hit by the load scan are recorded here (persistent JSON, manual review entry point)
    global _artifact_quarantine
    if _artifact_quarantine is None:
        path = os.environ.get("FUSION_COWORK_ARTIFACT_QUARANTINE", "").strip()
        if not path:
            path = os.path.join(os.path.expanduser("~"), ".fusion-cowork", "web", "artifact_quarantine.json")
        _artifact_quarantine = ArtifactQuarantine(path=path)
    return _artifact_quarantine


def scan_or_quarantine(
    text: str,
    source: str,
    kind: str,
    quarantine: Optional[ArtifactQuarantine] = None,
) -> Tuple[bool, Any]:
    # issue #554: pre-load scan — returns (clean, ScanReport); unclean artifacts go to quarantine when one is provided
    report = scan_artifact(text, source=source, kind=kind)
    if not report.clean and quarantine is not None:
        try:
            entry = quarantine.add(report, preview=text[:200])
            logger.warning(f"artifact quarantined: id={entry.get('id')} source={source} kind={kind}")
        except Exception as e:
            logger.warning(f"artifact quarantine write failed (scan result stands): {e}")
    return report.clean, report


def parse_llm_json(text: str, context: str = "") -> Any:
    # issue #554: LLM output JSON repair ladder — strict parse failure goes through the kernel ladder (code fences/
    # trailing commas/truncation closure); total failure raises JSONRepairError (typed error, never silently returns bad data)
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            pass
    value = parse_repair(text)
    bucket = json_repairs_in_step.get(None)
    if bucket is not None:
        bucket[0] += 1
    logger.info(f"json repair ladder applied ({context or 'unknown'}): input={str(text)[:120]!r}")
    return value


__all__ = [
    "JSONRepairError",
    "KernelPermissionBridge",
    "build_approval_gate",
    "build_sandbox_policy",
    "classify_destructive",
    "get_quarantine",
    "get_sandbox_runner",
    "is_spec_failure",
    "json_repairs_in_step",
    "parse_llm_json",
    "reset_sandbox_runner_cache",
    "attribute_node_failure",
    "failure_attribution_enabled",
    "sandbox_block_reason",
    "scan_or_quarantine",
]
