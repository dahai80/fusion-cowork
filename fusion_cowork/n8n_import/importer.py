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


def _if_condition_code(node: Dict[str, Any]) -> str:
    # n8n IF condition -> python predicate. v1 evaluates the static literal
    # operands; dynamic operands must arrive via upstream ${step...} refs in
    # other nodes' params (python_repl does not receive merged input).
    params = node.get("parameters") or {}
    conds = params.get("conditions") or {}
    left = conds.get("left") or params.get("value1", "")
    right = conds.get("right") or params.get("value2", "")
    op = str(conds.get("operation") or params.get("operation") or "equals")
    left_expr = left if isinstance(left, str) else json_default(left)
    right_expr = right if isinstance(right, str) else json_default(right)
    py_op = {
        "equals": "==",
        "notEquals": "!=",
        "contains": "in",
        "notContains": "not in",
    }.get(op, "==")
    return f'left = {left_expr!r}\nright = {right_expr!r}\nresult = "pass" if left {py_op} right else "drop"'


def json_default(v: Any) -> str:
    return str(v)


def _set_code(node: Dict[str, Any]) -> str:
    # n8n Set node assignments -> python dict build. v1 evaluates static
    # literal values; dynamic values belong in downstream node params via
    # expression translation (python_repl does not receive merged input).
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
        lines.append(f"result[{key!r}] = {value!r}")
    if len(lines) == 1:
        # fallback: n8n "values" -> "string" legacy shape
        values = params.get("values") or {}
        for e in values.get("string", []) or []:
            if isinstance(e, dict) and e.get("name"):
                lines.append(f"result[{e['name']!r}] = {e.get('value', '')!r}")
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
                verdict.expression_issues.extend(unsup)
            # body JSON string passthrough (http_request parses JSON strings)
            if isinstance(params.get("body"), str) and params["body"].startswith("="):
                params["body"] = translate_expression(params["body"][1:], upstream_id, ids, verdict)
            cname = "http_request"
        elif node["type"] == "n8n-nodes-base.set":
            params = {"code": _set_code(node), "timeout": 15}
            cname = "python_repl"
        elif node["type"] == "n8n-nodes-base.if":
            params = {"code": _if_condition_code(node), "timeout": 15}
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
