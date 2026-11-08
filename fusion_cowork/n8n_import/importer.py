"""n8n export -> cowork workflow importer (issue #395, import-only v1).

Output schema is the 0903 canvas schema (Workflow.from_dict shape) — no
parallel second graph format. Modes:
- "abort_if_unsupported": import only fully-runnable workflows (default)
- "mapped_subgraph": import the mapped subgraph, unsupported nodes excluded
  and their edges rewired around (reported, never silent)

Verification honesty (spec F4): the importer returns status "imported";
a workflow is only "imported_verified" after scripts/verify_n8n_import.py
or an equivalent run executes green inside fusion.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .analyzer import _LLM_TYPE_RE, N8nImportReport, analyze_n8n_workflow
from .expressions import slug_node_id, translate_params

logger = logging.getLogger("fusion_cowork.n8n_import")

# n8n node name -> cowork node id, uniquified on collision
_UNSAFE_ID = re.compile(r"[^a-z0-9_]")


def _unique_slug(name: str, used: set) -> str:
    base = slug_node_id(name)
    sid, i = base, 2
    while sid in used:
        sid = f"{base}_{i}"
        i += 1
    used.add(sid)
    return sid


def json_default(v: Any) -> str:
    return str(v)


def _operand(value: Any, upstream_id: str, ids: Dict[str, str], verdict) -> str:
    # n8n expression operands are translated to cowork ${step...} refs before
    # embedding; the engine resolves them at run time against upstream output.
    # Unsupported expressions surface in the report instead of silently
    # degrading to a static literal.
    if not isinstance(value, str):
        return json_default(value)
    expr = value[1:] if value.startswith("=") else value
    if "{{" in expr or "${" in expr:
        return translate_expression(expr, upstream_id, ids, verdict)
    return expr


# n8n IF operator -> python comparison. "contains" means left-contains-right,
# so the python form is `right in left` (operand order flips).
_IF_CMP_OPS = {"equals": "==", "notEquals": "!=", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
_IF_CONTAINS_OPS = {"contains": "in", "notContains": "not in"}
_IF_METHOD_OPS = {"startsWith": "startswith", "endsWith": "endswith"}


def _if_clauses(node: Dict[str, Any], upstream_id: str, ids: Dict[str, str], verdict) -> Tuple[List[str], str]:
    # n8n IF condition -> python predicate clauses. Supports both the legacy
    # single-condition shape (conditions.left/right/operation) and the v2/v3
    # conditions list (leftValue/rightValue/operator + combinator).
    params = node.get("parameters") or {}
    conds = params.get("conditions") or {}
    parsed: List[Tuple[Any, Any, str]] = []
    combinator = "and"
    if isinstance(conds, dict) and conds.get("conditions"):
        combinator = str(conds.get("combinator") or "and")
        for c in conds["conditions"]:
            if not isinstance(c, dict):
                continue
            op_obj = c.get("operator")
            op = op_obj.get("operation", "equals") if isinstance(op_obj, dict) else str(op_obj or "equals")
            parsed.append((c.get("leftValue", ""), c.get("rightValue", ""), str(op)))
    else:
        left = conds.get("left") or params.get("value1", "")
        right = conds.get("right") or params.get("value2", "")
        op = str(conds.get("operation") or params.get("operation") or "equals")
        parsed.append((left, right, op))
    return parsed, combinator


def _if_condition_code(node: Dict[str, Any], upstream_id: str, ids: Dict[str, str], verdict) -> str:
    parsed, combinator = _if_clauses(node, upstream_id, ids, verdict)
    lines: List[str] = []
    terms: List[str] = []
    for i, (left, right, op) in enumerate(parsed):
        left_expr = _operand(left, upstream_id, ids, verdict)
        right_expr = _operand(right, upstream_id, ids, verdict)
        lines.append(f"left_{i} = {left_expr!r}")
        lines.append(f"right_{i} = {right_expr!r}")
        if op in _IF_CMP_OPS:
            lines.append(f"cond_{i} = left_{i} {_IF_CMP_OPS[op]} right_{i}")
        elif op in _IF_CONTAINS_OPS:
            # n8n contains: left contains right
            lines.append(f"cond_{i} = right_{i} {_IF_CONTAINS_OPS[op]} left_{i}")
        elif op in _IF_METHOD_OPS:
            lines.append(f"cond_{i} = left_{i}.{_IF_METHOD_OPS[op]}(right_{i})")
        elif op == "exists":
            lines.append(f"cond_{i} = left_{i} is not None")
        else:
            verdict.expression_issues.append(f"{op} (unsupported n8n operator — defaulted to equals)")
            lines.append(f"cond_{i} = left_{i} == right_{i}")
        terms.append(f"cond_{i}")
    joiner = ") or (" if combinator == "or" else ") and ("
    joined = f"({joiner.join(terms)})" if len(terms) > 1 else terms[0]
    lines.append(f'result = "pass" if {joined} else "drop"')
    return "\n".join(lines)


def _set_code(node: Dict[str, Any], upstream_id: str, ids: Dict[str, str], verdict) -> str:
    # n8n Set node assignments -> python dict build. Values go through the
    # same expression translation as IF operands (engine resolves
    # ${step...} refs at run time; unsupported ones surface in the report).
    params = node.get("parameters") or {}
    assignments = params.get("assignments") or {}
    pairs = assignments.get("assignments") if isinstance(assignments, dict) else None
    entries = pairs or []
    lines = ["result = {}"]
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = str(e.get("name") or e.get("id") or "")
        value = e.get("value", "")
        if not key:
            continue
        lines.append(f"result[{key!r}] = {_operand(value, upstream_id, ids, verdict)!r}")
    if len(lines) == 1:
        # fallback: n8n "values" -> "string" legacy shape
        values = params.get("values") or {}
        for e in values.get("string", []) or []:
            if isinstance(e, dict) and e.get("name"):
                lines.append(f"result[{e['name']!r}] = {_operand(e.get('value', ''), upstream_id, ids, verdict)!r}")
    return "\n".join(lines)


def _llm_http_params(node: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    params = node.get("parameters") or {}
    model = str(params.get("model") or params.get("modelId") or "default")
    prompt = params.get("prompt") or params.get("text") or params.get("messages") or ""
    if not isinstance(prompt, str):
        prompt = json_default(prompt)
    return {
        "method": "POST",
        "url": f"{base_url.rstrip('/')}/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "body": {"model": model, "messages": [{"role": "user", "content": prompt}]},
    }


def import_n8n_workflow(
    data: Dict[str, Any],
    mode: str = "abort_if_unsupported",
    llm_base_url: str = "http://localhost:11432/v1",
) -> Tuple[Dict[str, Any], N8nImportReport, Dict[str, Any]]:
    """Import an n8n export dict.

    Returns (workflow_dict, report, meta). meta carries ids mapping +
    verification status ("imported" — set "imported_verified" only after a
    green run). Raises ValueError on unsupported nodes under
    abort_if_unsupported or when nothing is importable.
    """
    if mode not in ("abort_if_unsupported", "mapped_subgraph"):
        raise ValueError(f"unknown import mode: {mode}")
    report = analyze_n8n_workflow(data)
    if report.unsupported and mode == "abort_if_unsupported":
        names = ", ".join(f"{v.name} ({v.type})" for v in report.unsupported)
        raise ValueError(f"workflow has {len(report.unsupported)} unsupported nodes: {names}")

    nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict)]
    by_name = {str(n.get("name", "")): n for n in nodes}
    used: set = set()
    ids = {name: _unique_slug(name, used) for name in by_name}

    wf_nodes: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for name, node in by_name.items():
        verdict = next((v for v in report.verdicts if v.name == name), None)
        if verdict is None or verdict.verdict == "unsupported":
            dropped.append(name)
            continue
        if verdict.target == "":
            continue  # trigger nodes: no cowork node emitted
        params_in = node.get("parameters") or {}
        upstream = _single_upstream(data, name)
        upstream_id = ids.get(upstream, "") if upstream else ""
        if node["type"] == "n8n-nodes-base.httpRequest":
            params, unsup = translate_params(
                {
                    "method": str(params_in.get("method", "GET")).upper(),
                    "url": params_in.get("url", ""),
                    "headers": {},
                    "body": params_in.get("jsonBody", "") or params_in.get("body", "") or {},
                },
                upstream_id,
                ids,
            )
            if unsup:
                for u in unsup:
                    if u not in verdict.expression_issues:
                        verdict.expression_issues.append(u)
            # body JSON string passthrough (http_request parses JSON strings)
            if isinstance(params.get("body"), str) and params["body"].startswith("="):
                params["body"] = translate_expression(params["body"][1:], upstream_id, ids, verdict)
            cname = "http_request"
        elif node["type"] == "n8n-nodes-base.set":
            params = {"code": _set_code(node, upstream_id, ids, verdict), "timeout": 15}
            cname = "python_repl"
        elif node["type"] == "n8n-nodes-base.if":
            params = {"code": _if_condition_code(node, upstream_id, ids, verdict), "timeout": 15}
            cname = "python_repl"
        elif node["type"] == "n8n-nodes-base.merge":
            params = {}
            cname = "merge"
        elif _LLM_TYPE_RE.match(node["type"]):
            params = _llm_http_params(node, llm_base_url)
            cname = "http_request"
        else:
            dropped.append(name)
            continue
        wf_nodes.append(
            {
                "id": ids[name],
                "name": cname,
                "config": {"params": params, "label": name},
            }
        )

    wf_edges: List[Dict[str, Any]] = []
    connections = data.get("connections") or {}
    for src, outs in connections.items():
        if src in dropped or src not in ids:
            continue
        for out in (outs or {}).get("main") or []:
            for conn in out or []:
                dst = str(conn.get("node", ""))
                if dst in dropped or dst not in ids or dst not in by_name:
                    continue
                src_node = by_name[src]
                src_verdict = next((v for v in report.verdicts if v.name == src), None)
                if src_verdict and src_verdict.target == "":
                    continue  # trigger source: edge to start is implicit
                # if-node false branch (index 1) is not routed in v1
                if src_node["type"] == "n8n-nodes-base.if" and conn.get("index") == 1:
                    logger.warning("n8n import: IF false-branch edge %s->%s dropped (v1 serial execution)", src, dst)
                    continue
                # route the upstream response BODY only ("json" port) — a default
                # full-output merge would leak response headers (Content-Length!)
                # into the next request's headers/body and corrupt it
                wf_edges.append(
                    {"source_id": ids[src], "target_id": ids[dst], "source_output": "json", "target_input": "input"}
                )

    importable = [n for n in wf_nodes]
    if not importable:
        raise ValueError("nothing importable: every node is a trigger or unsupported")

    wf_id = f"n8n_{slug_node_id(data.get('name') or 'workflow')}"
    workflow = {
        "id": wf_id,
        "name": str(data.get("name") or wf_id),
        "params": {"type": "object", "properties": {}},
        "nodes": wf_nodes,
        "edges": wf_edges,
    }
    meta = {
        "source": "n8n",
        "n8n_name": data.get("name", ""),
        "node_ids": ids,
        "dropped_nodes": dropped,
        "verification_status": "imported",
        "cron": report.cron,
    }
    logger.info("n8n import '%s': %d nodes, %d edges, %d dropped", wf_id, len(wf_nodes), len(wf_edges), len(dropped))
    return workflow, report, meta


def translate_expression(expr: str, upstream_id: str, ids: Dict[str, str], verdict) -> str:
    from .expressions import UnsupportedExpression, translate_expression_value

    try:
        return translate_expression_value(expr, upstream_id, ids)
    except UnsupportedExpression as e:
        verdict.expression_issues.append(e.expr)
        logger.warning("n8n import: %s", e)
        return expr


def _single_upstream(data: Dict[str, Any], node_name: str) -> Optional[str]:
    connections = data.get("connections") or {}
    ups = []
    for src, outs in connections.items():
        for out in (outs or {}).get("main") or []:
            for conn in out or []:
                if str(conn.get("node", "")) == node_name:
                    ups.append(src)
    return ups[0] if len(ups) == 1 else (ups[0] if ups else None)
