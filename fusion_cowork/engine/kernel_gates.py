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

from ..security.kernel_sandbox import kernel_sandbox_policy
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
    from fusion_harness.executor_backend import select_sandbox_runner
except ImportError as exc:
    raise harness_import_error(exc) from exc

# 修复计数 — engine 在节点执行前 reset, 节点内的 parse_llm_json 递增;
# 计数随 node_finished 遥测上报 (issue #554: repairs in run telemetry)。
# ContextVar 持可变 list (asyncio.wait_for 建 task 复制 context — int set 不回传,
# 共享 list 原地 += 父协程可见); 未 set = 节点上下文外, 递增为 no-op
json_repairs_in_step: ContextVar = ContextVar("json_repairs_in_step")


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
    runner, _backend = get_sandbox_runner()
    if tiers:
        logger.info(f"sandbox policy wired: tiers={sorted(tiers)} runner={'yes' if runner else 'none (fail-closed)'}")
    # risk_threshold=inf — 权限闸门只认显式 tier 映射 (opt-in); 内核按风险分自动
    # 升 SANDBOX 的默认语义会让未配置的高风险工具 (shell_exec rm -rf 等) 在
    # guard 委托之前被 fail-closed 拦掉, 破坏 #73 既有行为
    # issue #570: 经 kernel_sandbox_policy 构建 (helper 的唯一生产调用点, 不再并行造 policy)
    return kernel_sandbox_policy(runner, tool_tiers=tiers, risk_threshold=float("inf"))


# issue #570: harness select_sandbox_runner 接线 — FUSION_SANDBOX_BACKEND=
# fusion_executor 时 impl 节点经 FusionExecutorBackend (原生沙箱 + Git 快照
# 回滚); 未设置/不可用 → 现行 runner 原样返回 (default 路径零行为变化)。
# 按 cwd 缓存单例 — 权限闸门每次 check 都会 build policy, 不缓存的话每个
# tool call 都新建 backend, 快照状态 (last_snapshot_id) 会互相覆盖。
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
    # 测试/配置变更后清缓存 (env 变了缓存里的 runner 不再匹配)
    _sandbox_runner_cache.clear()


# ── issue #571: MAST failure attribution 接线 (fusion_harness.failure_attribution) ──
# 节点失败时对当前 run 轨迹做归因, verdict (mode/cluster/confidence/evidence) 落
# 节点结果 + run 摘要; spec-class (specification_system_design) 失败不再烧重试
# 预算 (升级语义 — 归因随 run 摘要交 QA/rework 路由)。env 默认关 = 行为零变化。
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
    # MAST 归因 — 轨迹 = 已完成步 (tool_result) + 失败节点 error 记录;
    # 分析/分类语义全在内核 FailureAnalyzer, 这里只做记录形状适配
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
    # SANDBOX tier + 无 runner = BLOCKED (fail-closed, 审批也救不回); 其余放行 ""
    # 风险分/band 不参与 — 权限闸门只认显式 tier 映射 (opt-in); 内核按风险自动
    # 升 SANDBOX 的默认语义会把未配置的高风险工具 (shell_exec rm -rf 等) 在
    # #73 guard 委托之前 fail-closed 拦掉, 破坏既有行为
    decision = policy.decide(tool_name, params, (0.0, ""))
    if decision.tier is ExecutionTier.BLOCKED:
        return decision.reason or "sandbox tier required but no runner wired"
    return ""


_artifact_quarantine: Optional[ArtifactQuarantine] = None


def get_quarantine() -> ArtifactQuarantine:
    # 进程级隔离区单例 — 载入扫描命中的产物记录在此 (持久 JSON, 人工复核入口)
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
