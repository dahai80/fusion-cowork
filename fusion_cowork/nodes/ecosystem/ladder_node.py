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


@register_node
class WorkflowLadderNode(BaseNode):
    """阶梯编排 — 多阶段 worker 梯队 (fusion-harness ParallelTeam 原语)。

    stages: [{name, workers: [...]}] — 逐阶段串行推进, 每阶段 worker 并行
    (ParallelTeam), 阶段产出摘要注入下一阶段 prompt (阶梯传播), 阶段失败
    即 FAILED (allow_partial=true 放行继续)。
    """

    name = "workflow_ladder"
    display_name = "阶梯编排"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Run staged worker ladders via harness ParallelTeam; stage summaries feed the next stage"
    icon = "🪜"
    default_label = "阶梯编排"

    inputs = [
        {"key": "stages", "label": "阶段规格列表", "type": "array"},
    ]
    outputs = [
        {"key": "stages", "label": "各阶段结果", "type": "array"},
        {"key": "total", "label": "worker 总数", "type": "number"},
        {"key": "failed", "label": "失败数", "type": "number"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "stages": {
                    "type": "array",
                    "description": "阶段列表 [{name, workers: [{name, prompt, tool_allowlist?, "
                    "permission_mode?, budget_tokens?, max_turns?, worker_node?, worker_params?}]}]",
                },
                "allow_partial": {"type": "boolean", "description": "阶段内部分失败不整体 FAILED", "default": False},
                "propagate_context": {"type": "boolean", "description": "上阶段摘要注入下阶段 prompt", "default": True},
                "timeout": {
                    "type": "number",
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
            return NodeResult(status=NodeStatus.FAILED, error="workflow_ladder 参数 stages 必填 (非空数组)")
        try:
            runner = _load_runner()
        except ImportError as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e))

        from fusion_harness import ParallelTeam, SubagentSpec

        try:
            timeout = float(params.get("timeout", _DEFAULT_STAGE_TIMEOUT) or 0)
        except (TypeError, ValueError):
            timeout = _DEFAULT_STAGE_TIMEOUT
        allow_partial = bool(params.get("allow_partial", False))
        propagate = bool(params.get("propagate_context", True))
        team = ParallelTeam(runner)

        async def agent_fn(spec: Any, session: Any) -> str:
            session.record_turn()
            worker_node = spec.metadata.get("worker_node")
            if worker_node:
                node = NodeRegistry.create(
                    str(worker_node), config=NodeConfig(params=dict(spec.metadata.get("worker_params") or {}))
                )
                if node is None:
                    raise ValueError(f"worker '{spec.name}' 的 worker_node '{worker_node}' 未注册")
                res = await node.execute(
                    {"prompt": spec.prompt, "worker": spec.name, "session_id": session.subagent_id}
                )
                if _status_str(res.status) != "success":
                    raise RuntimeError(f"worker_node '{worker_node}' 执行失败: {res.error or '无错误详情'}")
                return str(res.summary or "worker_node 完成")
            return f"worker {spec.name} acknowledged prompt ({len(spec.prompt)} chars)"

        logger.info(f"workflow_ladder 开始: stages={len(stages)} timeout={timeout:g}s propagate={propagate}")
        stage_reports: List[Dict[str, Any]] = []
        prior_summaries = ""
        total = 0
        failed_total = 0
        for idx, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                return NodeResult(status=NodeStatus.FAILED, error=f"workflow_ladder 第 {idx} 阶段必须是对象")
            stage_name = str(stage.get("name") or f"stage-{idx}")
            workers = stage.get("workers")
            if not isinstance(workers, list) or not workers:
                return NodeResult(status=NodeStatus.FAILED, error=f"workflow_ladder 阶段 '{stage_name}' 缺 workers")
            total += len(workers)
            logger.info(
                f"workflow_ladder stage '{stage_name}' 开始: workers={len(workers)} (stage {idx}/{len(stages)})"
            )
            specs = []
            for w_idx, w in enumerate(workers, start=1):
                if not isinstance(w, dict):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"workflow_ladder 阶段 '{stage_name}' 第 {w_idx} 个 worker 必须是对象",
                    )
                name = str(w.get("name") or f"{stage_name}-worker-{w_idx}")
                prompt = str(w.get("prompt") or "")
                if not prompt:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"workflow_ladder 阶段 '{stage_name}' worker '{name}' 缺 prompt",
                    )
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
                logger.error(f"workflow_ladder stage '{stage_name}' 超时 ({timeout:g}s)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"workflow_ladder 阶段 '{stage_name}' 超时 ({timeout:g}s)",
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            except ImportError as e:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=str(e),
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            workers_out = [
                {
                    "name": r.name,
                    "subagent_id": r.subagent_id,
                    "status": r.status,
                    "summary": r.summary,
                    "error": r.error,
                }
                for r in raw
            ]
            failed = [r for r in workers_out if r["status"] != "completed"]
            failed_total += len(failed)
            stage_reports.append({"name": stage_name, "results": workers_out, "failed": len(failed)})
            if failed and not allow_partial:
                names = ",".join(r["name"] for r in failed)
                logger.error(f"workflow_ladder stage '{stage_name}' 失败: {names}")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"阶段 '{stage_name}' {len(failed)}/{len(raw)} worker 失败: {names}",
                    data={"stages": stage_reports, "total": total, "failed": failed_total},
                )
            # 阶梯传播 — 本阶段完成 worker 的摘要喂下一阶段
            prior_summaries = "\n".join(
                f"[{r['name']}] {r['summary']}" for r in workers_out if r["status"] == "completed"
            )
            logger.info(f"workflow_ladder stage '{stage_name}' 完成: {len(raw)} workers, {len(failed)} failed")

        logger.info(f"workflow_ladder 完成: total={total} failed={failed_total}")
        data = {"stages": stage_reports, "total": total, "failed": failed_total}
        if failed_total:
            # allow_partial 放行的失败 — 结果仍然诚实暴露计数
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=data,
                error=f"{failed_total}/{total} worker 失败 (allow_partial)",
                summary=f"{len(stages)} 阶段完成 ({total} worker, {failed_total} 失败)",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data=data,
            summary=f"{len(stages)} 阶段完成 ({total} worker, 0 失败)",
        )
