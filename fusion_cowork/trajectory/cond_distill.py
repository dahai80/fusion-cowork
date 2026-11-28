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

Compile one static plan: an ordered list of steps. Each step: {"node": "<node_type>", "id": "<instance_id>", "upstream": ["<instance_id>", ...], "when": <condition or null>}. A condition is one of:
{"var": "<instance_id>.<output.path>", "gte": number} | {"var": "...", "lte": number} | {"var": "...", "eq": value} | {"var": "...", "neq": value}
The referenced var is the output of an earlier step's instance. A step with a "when" condition runs only if the condition holds; if the var is missing, the step is skipped. Steps without "when" always run.

Rules: "id" is the trajectory record's instance id (unique per node instance) and "node" is the node type used to instantiate it; copy both from the evidence. "upstream" copies the recorded upstream instance ids — preserve the recorded fan-out/fan-in shape exactly (two steps sharing the same upstream form a fan-out; a step listing two upstreams is a merge). If a record has no id, use its node name as the id and omit "upstream". Include only instances that appear in the trajectories. Order steps so every var referenced by a "when" is produced by an earlier step. Guard error-prone nodes with conditions inferred from the failure evidence. Reply with JSON only: {"steps": [...]}"""


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
                "id": str(get("node_id", "") or get("node_name", "") or ""),
                "upstream": [str(u) for u in (get("upstream", None) or [])],
                "input_bindings": dict(get("input_bindings", None) or {}),
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
    # issue #638 follow-up: port routing is deterministic, not an LLM decision.
    # Recorded input bindings (port -> [source_id, source_output]) are copied
    # onto the plan steps; plan_to_workflow emits routed edges from them.
    bindings_by_id = {
        str(e.get("id", "") or ""): e.get("input_bindings") or {} for e in evidence if isinstance(e, dict)
    }
    for s in clean:
        b = bindings_by_id.get(str(s.get("id", "") or ""), {})
        if b:
            s["inputs"] = {str(port): f"{src}.{out}" for port, (src, out) in b.items()}
    logger.info("cond_distill: compiled %s steps", len(clean))
    return {"steps": clean}


def plan_to_workflow(plan: Dict[str, Any], create_node: Any, workflow: Any) -> List[str]:
    # Compile a condition-DSL plan into an existing Workflow: nodes with "when"
    # conditions inverted into NodeConfig.skip_if
    # ({"source": producer_node_id, "path": ..., gte/lte/eq/neq, negate: true})
    # so the engine's data-driven skip (issue #422) executes the branch.
    # issue #638: steps carrying instance ids + recorded upstream lists compile
    # back as the recorded DAG (fan-out/fan-in preserved); steps without them
    # keep the legacy linear chain (prev_id chaining).
    # create_node(name) -> BaseNode instance (caller wires the registry).
    steps = [
        s
        for s in (plan.get("steps") if isinstance(plan, dict) else None) or []
        if isinstance(s, dict) and str(s.get("node", "") or "")
    ]
    has_dag_shape = any(str(s.get("id", "") or "") or s.get("upstream") for s in steps)
    node_ids: Dict[str, str] = {}
    name_index: Dict[str, str] = {}
    order: List[str] = []
    prev_id = ""
    for step in steps:
        name = str(step.get("node", "") or "")
        inst_id = str(step.get("id", "") or "") or name
        if has_dag_shape and inst_id in node_ids:
            # duplicate instance id — impossible in a recorded trajectory; skip the replay
            logger.warning("cond_distill: duplicate instance id %s in plan, skipped", inst_id)
            continue
        node = create_node(name)
        if has_dag_shape and inst_id:
            # instance identity must survive compilation: engine ids are
            # generated (node_<hex>) — pin the recorded instance id so edges
            # and skip_if sources resolve (issue #638)
            node.id = inst_id
        cond = step.get("when")
        if isinstance(cond, dict) and cond.get("var"):
            var = _extract_condition_var(cond)
            parts = var.split(".", 1)
            source_key = parts[0]
            path = parts[1] if len(parts) > 1 else ""
            source_id = node_ids.get(source_key, "") or name_index.get(source_key, "")
            inv: Dict[str, Any] = {"path": path, "negate": True}
            if source_id:
                inv["source"] = source_id
            for op in ("gte", "lte", "eq", "neq"):
                if op in cond:
                    inv[op] = cond[op]
                    break
            node.config.skip_if = [inv]
        added = workflow.add_node(node)
        node_ids[inst_id] = added
        name_index.setdefault(name, added)
        order.append(added)
        if has_dag_shape:
            upstream = [str(u) for u in (step.get("upstream") or []) if str(u) in node_ids]
            # issue #638 follow-up: explicit port routing. "input" names the
            # upstream output field this step consumed; the target port is
            # resolved deterministically from the node's declared inputs
            # (first declared input port). Upstreams with no routing keep the
            # default whole-output merge edge. "inputs" (port->var map) is
            # also honored for callers that know the port names.
            routing: Dict[str, Dict[str, str]] = {}
            declared = [
                i.get("key") for i in (getattr(node, "inputs", None) or []) if isinstance(i, dict) and i.get("key")
            ]
            single = step.get("input")
            if isinstance(single, str) and "." in single:
                src_key, out_key = single.split(".", 1)
                if src_key in node_ids:
                    routing.setdefault(src_key, {})[declared[0] if declared else "data"] = out_key
            for port, var in (step.get("inputs") or {}).items():
                if not isinstance(var, str) or "." not in var:
                    continue
                src_key, out_key = var.split(".", 1)
                if src_key in node_ids:
                    routing.setdefault(src_key, {})[port] = out_key
            if upstream:
                for u in upstream:
                    mapped = routing.get(u, {})
                    if mapped:
                        for port, out_key in mapped.items():
                            workflow.connect(node_ids[u], added, source_output=out_key, target_input=port)
                    else:
                        workflow.connect(node_ids[u], added)
            elif prev_id:
                # no recorded upstream (or upstream outside the plan) — chain to the
                # previous compiled node so the step stays reachable in topo order
                workflow.connect(prev_id, added)
        elif prev_id:
            workflow.connect(prev_id, added)
        prev_id = added
    logger.info("cond_distill: workflow compiled with %s nodes (dag_shape=%s)", len(order), has_dag_shape)
    return order
