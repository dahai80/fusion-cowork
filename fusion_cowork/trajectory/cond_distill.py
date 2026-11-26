import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# exp/cond-distill: flywheel condition extraction (ReTrace, arXiv 2611.14492).
# Orch-bench evidence (factory-software/reports/2026-11-26-orchbench-dynamic-vs-static.md):
# order-statistics distillation fails (45.8%/33.3% vs 75% designed); LLM condition
# extraction recovers the workflow exactly, and compiled-plan + one-shot repair
# reaches 100% at ~1/64 the token cost of free-order orchestration.

CONDITION_SYSTEM_PROMPT = """You are a workflow compiler. You receive per-run trajectories from a workflow engine: ordered node executions with outputs (errors included) and the final run status. Nodes have hidden preconditions you must infer.

Compile one static plan: an ordered list of steps. Each step: {"node": "<node_name>", "when": <condition or null>}. A condition is one of:
{"var": "<node_name>.<output.path>", "gte": number} | {"var": "...", "lte": number} | {"var": "...", "eq": value} | {"var": "...", "neq": value}
The referenced var is the output of an earlier step's node. A step with a "when" condition runs only if the condition holds; if the var is missing, the step is skipped. Steps without "when" always run.

Rules: include only node names that appear in the trajectories. Order steps so every var referenced by a "when" is produced by an earlier step. Guard error-prone nodes with conditions inferred from the failure evidence. Reply with JSON only: {"steps": [...]}"""


def build_evidence(steps: List[Any], run_success: bool) -> List[Dict[str, Any]]:
    # Ordered node executions -> LLM evidence packet. Accepts workflow step
    # objects (node_name/output_data/error/status) or plain dicts.
    out: List[Dict[str, Any]] = []
    for s in steps or []:
        if isinstance(s, dict):
            get = s.get
        else:
            step = s

            def get(k, d=None, step=step):
                return getattr(step, k, d)

        out.append(
            {
                "node": str(get("node_name", "") or ""),
                "status": str(get("status", "") or ""),
                "output": _json_safe_output(get("output_data", None)),
                "error": str(get("error", "") or "")[:300],
            }
        )
    return out


def _json_safe_output(output: Any) -> Any:
    if output is None or isinstance(output, bool):
        return output
    if isinstance(output, (int, float)):
        return output
    if isinstance(output, str):
        return output[:200]
    if isinstance(output, dict):
        return {str(k): _json_safe_output(v) for k, v in list(output.items())[:20]}
    if isinstance(output, (list, tuple)):
        return [_json_safe_output(v) for v in output[:20]]
    return str(output)[:200]


def _extract_condition_var(cond: Dict[str, Any]) -> str:
    return str(cond.get("var", "") or "")


async def extract_plan(client: Any, evidence: List[Dict[str, Any]], model: str = "") -> Dict[str, Any]:
    # One LLM call compiles trajectories into a condition-DSL plan.
    # Caller picks model explicitly; empty model falls back to
    # FUSION_MLX_MODEL env, then the fleet default chat model.
    messages = [
        {"role": "system", "content": CONDITION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, default=str)[:12000]},
    ]
    resolved_model = (model or os.environ.get("FUSION_MLX_MODEL", "") or "Qwen3.8-27B-4bit").strip()
    resp = await client.chat(model=resolved_model, messages=messages, temperature=0.0, max_tokens=1500)
    content = resp.content if hasattr(resp, "content") else str(resp)
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        logger.warning("cond_distill: no JSON in LLM reply, empty plan")
        return {"steps": []}
    try:
        plan = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("cond_distill: LLM reply is not valid JSON, empty plan")
        return {"steps": []}
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return {"steps": []}
    clean = [s for s in steps if isinstance(s, dict) and str(s.get("node", "") or "")]
    logger.info("cond_distill: compiled %s steps", len(clean))
    return {"steps": clean}


def plan_to_workflow(plan: Dict[str, Any], create_node: Any, workflow: Any) -> List[str]:
    # Compile a condition-DSL plan into an existing Workflow: linear chain of
    # nodes, with "when" conditions inverted into NodeConfig.skip_if
    # ({"source": producer_node_id, "path": ..., gte/lte/eq/neq, negate: true})
    # so the engine's data-driven skip (issue #422) executes the branch.
    # create_node(name) -> BaseNode instance (caller wires the registry).
    node_ids: Dict[str, str] = {}
    prev_id = ""
    steps = plan.get("steps") if isinstance(plan, dict) else None
    for step in steps or []:
        name = str(step.get("node", "") or "")
        if not name:
            continue
        node = create_node(name)
        cond = step.get("when")
        if isinstance(cond, dict) and cond.get("var"):
            var = _extract_condition_var(cond)
            parts = var.split(".", 1)
            source_name = parts[0]
            path = parts[1] if len(parts) > 1 else ""
            source_id = node_ids.get(source_name, "")
            inv: Dict[str, Any] = {"path": path, "negate": True}
            if source_id:
                inv["source"] = source_id
            for op in ("gte", "lte", "eq", "neq"):
                if op in cond:
                    inv[op] = cond[op]
                    break
            node.config.skip_if = [inv]
        node_ids[name] = workflow.add_node(node)
        if prev_id:
            workflow.connect(prev_id, node_ids[name])
        prev_id = node_ids[name]
    logger.info("cond_distill: workflow compiled with %s nodes", len(node_ids))
    return list(node_ids.values())
