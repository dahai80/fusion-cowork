# issue #470: workflow ladder — DAG/workflow-level orchestration built on
# fusion-harness kernel primitives (ParallelTeam over SubagentRunner).
# Does NOT duplicate SubagentRunner semantics: workers are harness subagents,
# stages run strictly sequentially, each stage's summaries feed the next
# stage's worker prompts (ladder propagation).

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)

logger = logging.getLogger(__name__)

_DEFAULT_STAGE_TIMEOUT = 600.0
_DEFAULT_BUDGET = 20000
_DEFAULT_MAX_TURNS = 8


def _status_str(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _load_runner() -> Any:
    try:
        from fusion_harness import SubagentRunner
    except ImportError as e:
        raise ImportError(
            "workflow_ladder requires fusion-harness (pip install 'fusion-cowork[plugins]') "
            "— kernel primitive SubagentRunner missing"
        ) from e
    return SubagentRunner()


def _ladder_phase_evaluator(results: Any) -> Dict[str, Any]:
    # issue #558 (#133): deterministic phase check — empty results or an
    # all-failed phase blocks the phase (kernel then marks surviving members
    # "blocked" and emits team_evaluator_block); partial failure stays ok so
    # allow_partial semantics remain consumer-side
    results = list(results or [])
    if not results:
        return {"ok": False, "reason": "empty phase results"}
    failed = [r for r in results if getattr(r, "status", "") == "failed"]
    if len(failed) == len(results):
        return {"ok": False, "reason": f"all {len(results)} members failed"}
    return {"ok": True, "reason": ""}


def _count_blocked(results: Any) -> int:
    return sum(1 for r in (results or []) if getattr(r, "status", "") == "blocked")


def _validate_model_tier(raw: Any) -> str:
    # issue #563 (#141): optional per-worker model_tier hint — validated
    # client-side against the kernel vocabulary BEFORE dispatch (typed error,
    # no partial fan-out); "" = kernel policy default, cowork never guesses
    from fusion_harness import VALID_MODEL_TIERS

    tier = str(raw or "").strip()
    if tier and tier not in VALID_MODEL_TIERS:
        raise ValueError(
            f"unknown model tier {tier!r} (valid: {', '.join(t for t in VALID_MODEL_TIERS if t)}, or empty)"
        )
    return tier


def _stage_fanout_warnings(workers: list) -> List[str]:
    # issue #563 (#124): pre-dispatch fan-out guidance on the node level —
    # above the complex band surfaces the kernel token-multiplier warning in
    # the result (kernel fanout_warnings wording, single source); above the
    # hard max the kernel validate_brief would reject, so fail fast here with
    # a typed error instead of mid-stage
    from types import SimpleNamespace

    from fusion_harness import FANOUT_HARD_MAX, fanout_warnings

    n = len(workers)
    if n > FANOUT_HARD_MAX:
        raise ValueError(f"stage concurrency {n} exceeds fan-out hard max {FANOUT_HARD_MAX} — split the stage")
    return fanout_warnings(SimpleNamespace(concurrency=n))


@register_node
class WorkflowLadderNode(BaseNode):
    """Ladder orchestration — multi-stage worker ladders (fusion-harness ParallelTeam primitive).

    stages: [{name, workers: [...]}] — stages advance serially, workers within a
    stage run in parallel (ParallelTeam), stage output summaries are injected into
    the next stage's prompt (ladder propagation), a failed stage fails the node
    (allow_partial=true lets it continue).
    """

    name = "workflow_ladder"
    display_name = "阶梯编排"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Run staged worker ladders via harness ParallelTeam; stage summaries feed the next stage"
    icon = "🪜"
    default_label = "阶梯编排"  # i18n: user-facing

    inputs = [
        # i18n: user-facing
        {"key": "stages", "label": "阶段规格列表", "type": "array"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "stages", "label": "各阶段结果", "type": "array"},
        # i18n: user-facing
        {"key": "total", "label": "worker 总数", "type": "number"},
        # i18n: user-facing
        {"key": "failed", "label": "失败数", "type": "number"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "stages": {
                    "type": "array",
                    # i18n: user-facing
                    "description": "阶段列表 [{name, workers: [{name, prompt, tool_allowlist?, "
                    "permission_mode?, budget_tokens?, max_turns?, worker_node?, worker_params?, "
                    "model_tier? (cheap|strong|空)}]}] — 单阶段 worker 数 >20 拒绝派发, >15 附 token 倍增警告",  # i18n: user-facing
                },
                # i18n: user-facing
                "allow_partial": {"type": "boolean", "description": "阶段内部分失败不整体 FAILED", "default": False},
                "phase_evaluator": {
                    "type": "boolean",
                    # i18n: user-facing
                    "description": "阶段评估器 (#558/#133) — 空结果/全失败阶段 fail-safe 拦截, 幸存成员标记 blocked",
                    "default": False,
                },
                # i18n: user-facing
                "propagate_context": {"type": "boolean", "description": "上阶段摘要注入下阶段 prompt", "default": True},
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "单阶段超时秒数 (0 = 不限)",
                    "default": _DEFAULT_STAGE_TIMEOUT,
                },
            },
            "required": ["stages"],
        }

    async def execute(self, inputs: dict) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        stages = params.get("stages") or inputs.get("stages")
        if not isinstance(stages, list) or not stages:
            return NodeResult(
                status=NodeStatus.FAILED, error="workflow_ladder 参数 stages 必填 (非空数组)"  # i18n: user-facing
            )  # i18n: user-facing
        try:
            runner = _load_runner()
        except ImportError as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e))

        from fusion_harness import ParallelTeam, SubagentSpec

        # issue #563: upfront spec validation — model_tier vocabulary + fan-out
        # bounds checked for EVERY stage before any dispatch (typed errors, no
        # partial fan-out left behind when a later stage's spec is invalid)
        for idx, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"workflow_ladder 第 {idx} 阶段必须是对象"  # i18n: user-facing
                )  # i18n: user-facing
            stage_name = str(stage.get("name") or f"stage-{idx}")
            workers = stage.get("workers")
            if not isinstance(workers, list) or not workers:
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"workflow_ladder 阶段 '{stage_name}' 缺 workers"  # i18n: user-facing
                )  # i18n: user-facing
            for w in workers:
                if not isinstance(w, dict):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"workflow_ladder 阶段 '{stage_name}' worker 必须是对象",  # i18n: user-facing
                    )
                try:
                    _validate_model_tier(w.get("model_tier"))
                except ValueError as e:
                    logger.error(f"workflow_ladder stage '{stage_name}' invalid model_tier: {e}")
                    return NodeResult(
                        status=NodeStatus.FAILED, error=f"workflow_ladder 阶段 '{stage_name}': {e}"  # i18n: user-facing
                    )  # i18n: user-facing
            try:
                _stage_fanout_warnings(workers)
            except ValueError as e:
                logger.error(f"workflow_ladder stage '{stage_name}' fan-out bound: {e}")
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"workflow_ladder 阶段 '{stage_name}': {e}"  # i18n: user-facing
                )  # i18n: user-facing

        try:
            timeout = float(params.get("timeout", _DEFAULT_STAGE_TIMEOUT) or 0)
        except (TypeError, ValueError):
            timeout = _DEFAULT_STAGE_TIMEOUT
        allow_partial = bool(params.get("allow_partial", False))
        propagate = bool(params.get("propagate_context", True))
        # issue #558 (#133): opt-in phase evaluator — cheap deterministic checks
        # between member phases (empty results / all-failed phase), rate-limiting
        # error compounding down the ladder; kernel marks surviving members
        # "blocked" and emits team_evaluator_block (fail-safe, never re-raised)
        evaluator = _ladder_phase_evaluator if bool(params.get("phase_evaluator", False)) else None
        team = ParallelTeam(runner, phase_evaluator=evaluator)

        async def agent_fn(spec: Any, session: Any) -> str:
            session.record_turn()
            worker_node = spec.metadata.get("worker_node")
            if worker_node:
                node = NodeRegistry.create(
                    str(worker_node), config=NodeConfig(params=dict(spec.metadata.get("worker_params") or {}))
                )
                if node is None:
                    raise ValueError(f"worker '{spec.name}' 的 worker_node '{worker_node}' 未注册")  # i18n: user-facing
                res = await node.execute(
                    {"prompt": spec.prompt, "worker": spec.name, "session_id": session.subagent_id}
                )
                if _status_str(res.status) != "success":
                    raise RuntimeError(
                        f"worker_node '{worker_node}' 执行失败: {res.error or '无错误详情'}"  # i18n: user-facing
                    )  # i18n: user-facing
                return str(res.summary or "worker_node 完成")  # i18n: user-facing
            return f"worker {spec.name} acknowledged prompt ({len(spec.prompt)} chars)"

        logger.info(f"workflow_ladder start: stages={len(stages)} timeout={timeout:g}s propagate={propagate}")
        stage_reports: List[Dict[str, Any]] = []
        prior_summaries = ""
        total = 0
        failed_total = 0
        for idx, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"workflow_ladder 第 {idx} 阶段必须是对象"  # i18n: user-facing
                )  # i18n: user-facing
            stage_name = str(stage.get("name") or f"stage-{idx}")
            workers = stage.get("workers")
            if not isinstance(workers, list) or not workers:
                return NodeResult(
                    status=NodeStatus.FAILED, error=f"workflow_ladder 阶段 '{stage_name}' 缺 workers"  # i18n: user-facing
                )  # i18n: user-facing
            total += len(workers)
            # issue #563: pre-dispatch fan-out guidance — validation already ran
            # upfront, so this cannot raise; warnings ride the stage report
            stage_warnings = _stage_fanout_warnings(workers)
            for w_text in stage_warnings:
                logger.warning(f"workflow_ladder stage '{stage_name}': {w_text}")
            logger.info(
                f"workflow_ladder stage '{stage_name}' start: workers={len(workers)} (stage {idx}/{len(stages)})"
            )
            specs = []
            for w_idx, w in enumerate(workers, start=1):
                if not isinstance(w, dict):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"workflow_ladder 阶段 '{stage_name}' 第 {w_idx} 个 worker 必须是对象",  # i18n: user-facing
                    )
                name = str(w.get("name") or f"{stage_name}-worker-{w_idx}")
                prompt = str(w.get("prompt") or "")
                if not prompt:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"workflow_ladder 阶段 '{stage_name}' worker '{name}' 缺 prompt",  # i18n: user-facing
                    )
                tier = _validate_model_tier(w.get("model_tier"))
                if prior_summaries and propagate:
                    prompt = f"{prompt}\n\nPrior stage results (ladder context):\n{prior_summaries}"
                specs.append(
                    SubagentSpec(
                        name=name,
                        prompt=prompt,
                        tool_allowlist=list(w.get("tool_allowlist") or []),
                        permission_mode=str(w.get("permission_mode") or "default"),
                        budget_tokens=int(w.get("budget_tokens") or _DEFAULT_BUDGET),
                        max_turns=int(w.get("max_turns") or _DEFAULT_MAX_TURNS),
                        metadata={
                            "worker_node": w.get("worker_node") or "",
                            "worker_params": dict(w.get("worker_params") or {}),
                            "model_tier": tier,
                        },
                    )
                )
            try:
                if timeout > 0:
                    raw = await asyncio.wait_for(
                        team.run(specs, agent_fn, parent_tools=["workflow_ladder"], parent_mode="default"),
                        timeout=timeout,
                    )
                else:
                    raw = await team.run(specs, agent_fn, parent_tools=["workflow_ladder"], parent_mode="default")
            except TimeoutError:
                logger.error(f"workflow_ladder stage '{stage_name}' timed out ({timeout:g}s)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    # i18n: user-facing
                    error=f"workflow_ladder 阶段 '{stage_name}' 超时 ({timeout:g}s)",
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            except ImportError as e:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=str(e),
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            tier_by_name = {s.name: str(s.metadata.get("model_tier") or "") for s in specs}
            workers_out = [
                {
                    "name": r.name,
                    "subagent_id": r.subagent_id,
                    "status": r.status,
                    "summary": r.summary,
                    "error": r.error,
                    "model_tier": tier_by_name.get(r.name, ""),
                }
                for r in raw
            ]
            failed = [r for r in workers_out if r["status"] != "completed"]
            failed_total += len(failed)
            # issue #558 (#133): evaluator-blocked survivors — honest counters in
            # the stage report + a loud log line (kernel emitted team_evaluator_block)
            blocked = _count_blocked(raw)
            if blocked:
                logger.error(
                    f"workflow_ladder stage '{stage_name}' phase evaluator blocked: {blocked}/{len(raw)} members marked blocked"
                )
            stage_reports.append(
                {
                    "name": stage_name,
                    "results": workers_out,
                    "failed": len(failed),
                    "blocked": blocked,
                    "warnings": stage_warnings,
                }
            )
            if failed and not allow_partial:
                names = ",".join(r["name"] for r in failed)
                logger.error(f"workflow_ladder stage '{stage_name}' failed: {names}")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    # i18n: user-facing
                    error=f"阶段 '{stage_name}' {len(failed)}/{len(raw)} worker 失败: {names}",
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            # Ladder propagation — completed workers' summaries from this stage feed the next stage
            prior_summaries = "\n".join(
                f"[{r['name']}] {r['summary']}" for r in workers_out if r["status"] == "completed"
            )
            logger.info(f"workflow_ladder stage '{stage_name}' done: {len(raw)} workers, {len(failed)} failed")

        logger.info(f"workflow_ladder done: total={total} failed={failed_total}")
        data = {"stages": stage_reports, "total": total, "failed": failed_total}
        if failed_total:
            # failures allowed through by allow_partial — the result still exposes honest counters
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=data,
                # i18n: user-facing
                error=f"{failed_total}/{total} worker 失败 (allow_partial)",
                # i18n: user-facing
                summary=f"{len(stages)} 阶段完成 ({total} worker, {failed_total} 失败)",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data=data,
            # i18n: user-facing
            summary=f"{len(stages)} 阶段完成 ({total} worker, 0 失败)",
        )
