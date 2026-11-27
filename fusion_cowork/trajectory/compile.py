import json
import logging
from typing import Any, Dict, List, Tuple

from fusion_cowork.trajectory.cond_distill import build_evidence, extract_plan, plan_to_workflow

logger = logging.getLogger(__name__)

# exp/flywheel-compile: flywheel full-chain productization (issue #623 target).
# Wires the three validated stages into one compile path:
#   trajectory steps -> build_evidence -> extract_plan (one LLM call)
#   -> plan_to_workflow (skip_if conditions on a real Workflow).
# Evidence base: factory-software fusion-horizon v3/v4 — compiled plans hit
# 5/5 held-out at ~128 tok/task vs 0/5 static floor; dynamic baseline pays
# ~24k tok/task per run. Exploration is paid once, compiled execution is
# deterministic and bounded.


async def compile_workflow(
    steps: List[Any],
    client: Any,
    create_node: Any,
    workflow: Any,
    model: str = "",
    run_success: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    # steps: ordered execution records (dicts with node_name/status/output_data/
    # error, or engine WorkflowStep objects). create_node(name) -> BaseNode
    # (caller wires the registry). Returns (plan, node_ids); empty steps or an
    # unparseable LLM reply yield an empty plan and no nodes (visible log).
    evidence = build_evidence(steps, run_success=run_success)
    if not evidence:
        logger.warning("flywheel_compile: no steps in input, empty plan")
        return {"steps": []}, []
    plan = await extract_plan(client, evidence, model=model)
    if not plan.get("steps"):
        logger.warning("flywheel_compile: extraction produced empty plan")
        return plan, []
    node_ids = plan_to_workflow(plan, create_node, workflow)
    logger.info("flywheel_compile: compiled %s nodes", len(node_ids))
    return plan, node_ids


def steps_from_jsonl(path: Any, run_id: str = "") -> List[Dict[str, Any]]:
    # Recover ordered execution steps from a persisted kernel trajectory JSONL
    # (TrajectoryWriter output). Filters post_node_execute records; output
    # data is recovered from payload.data.result.data (hook payload carries
    # the NodeResult, _json_safe expanded). run_id="" selects the last run in
    # file order; a given run_id selects that run's records only.
    target = str(run_id or "")
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("legacy_event", "") or "") != "post_node_execute":
                    continue
                rid = str(rec.get("run_id", "") or payload.get("execution_id", "") or "")
                if target and rid != target:
                    continue
                data = payload.get("data") or {}
                if not isinstance(data, dict):
                    data = {}
                result = data.get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                out = result.get("data")
                step = {
                    "node_name": str(payload.get("node_name", "") or ""),
                    "status": str(rec.get("status", "") or ""),
                    "output_data": out if isinstance(out, dict) else {},
                    "error": str(payload.get("error", "") or data.get("error", "") or ""),
                }
                if not step["node_name"]:
                    continue
                if rid not in groups:
                    groups[rid] = []
                    order.append(rid)
                groups[rid].append(step)
    except OSError as e:
        logger.warning("flywheel_compile: trajectory read failed: %s", e)
        return []
    if target:
        return groups.get(target, [])
    if not order:
        return []
    chosen = order[-1]
    logger.info("flywheel_compile: steps_from_jsonl selected run=%s (%s steps)", chosen, len(groups[chosen]))
    return groups[chosen]


def steps_from_snapshot(snapshot: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # TrajectoryRecorder.steps_snapshot entries carry node_id/node_name/status/
    # error/summary but no output_data (the POST hook payload keeps it inside
    # result). Map them to build_evidence shape; output stays empty, so use
    # this only when conditions do not reference output vars — prefer
    # steps_from_jsonl for full-fidelity compilation.
    out: List[Dict[str, Any]] = []
    for s in snapshot or []:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "node_name": str(s.get("node_name", "") or ""),
                "status": str(s.get("status", "") or ""),
                "output_data": {},
                "error": str(s.get("error", "") or ""),
            }
        )
    return out


class CompileError(Exception):
    # Domain error shared by the web route and the DeskRPC handler; each
    # caller maps code+message to its own transport convention.
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def compile_trajectory(body: Dict[str, Any], mlx: Any, create_node) -> Dict[str, Any]:
    # Shared compile surface (issue #623): trajectory JSONL by session_id
    # (+optional run_id) or an inline snapshot -> one condition-extraction
    # LLM call -> executable Workflow dict with skip_if conditions landed.
    # Raises CompileError with a stable code; transport layers map it.
    import re
    from pathlib import Path

    from fusion_cowork.engine.workflow import Workflow
    from fusion_cowork.trajectory.recorder import DEFAULT_TRAJECTORY_DIR

    session_id = str(body.get("session_id") or "").strip()
    run_id = str(body.get("run_id") or "").strip()
    model = str(body.get("model") or "").strip()
    workflow_name = str(body.get("workflow_name") or "").strip()
    snapshot = body.get("snapshot") if isinstance(body.get("snapshot"), list) else None
    if not session_id and not snapshot:
        raise CompileError("bad_request", "session_id 或 snapshot 必填")  # i18n: user-facing
    steps: List[Dict[str, Any]] = []
    if session_id:
        # session_id is the trajectory-file stem; whitelist chars block path traversal
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise CompileError("bad_request", "非法 session_id")  # i18n: user-facing
        path = Path(DEFAULT_TRAJECTORY_DIR) / f"{session_id}.jsonl"
        if not path.is_file():
            raise CompileError("not_found", f"轨迹文件不存在: {session_id}")  # i18n: user-facing
        steps = steps_from_jsonl(path, run_id=run_id)
    else:
        steps = steps_from_snapshot(snapshot or [])
    if not steps:
        raise CompileError("empty_trajectory", "轨迹中无可编译步骤 (无 post_node_execute 记录)")  # i18n: user-facing
    if mlx is None:
        raise CompileError("no_model_client", "模型客户端不可用")  # i18n: user-facing
    wf = Workflow(name=workflow_name or f"compiled-{session_id or 'snapshot'}")
    logger.info(
        "trajectory compile request: session=%s run=%s steps=%d model=%s",
        session_id or "-",
        run_id or "-",
        len(steps),
        model or "(auto)",
    )
    try:
        plan, node_ids = await compile_workflow(steps, mlx, create_node, wf, model=model)
    except CompileError:
        raise
    except Exception as e:
        logger.error("trajectory compile failed: %s", e)
        raise CompileError("compile_failed", f"编译失败: {e}") from e  # i18n: user-facing
    if not node_ids:
        raise CompileError("empty_plan", "条件抽取产出空计划 (轨迹无成功步骤或回复不可解析)")  # i18n: user-facing
    return {"plan": plan, "node_ids": node_ids, "steps_used": len(steps), "workflow": wf.to_dict()}
