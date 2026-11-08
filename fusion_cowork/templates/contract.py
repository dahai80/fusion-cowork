"""Template placeholder contract validation (issue #389, spec §3.1).

Field-name drift between pipeline components (n12 `${result.number}` vs
upstream `{pr_number}` shape) is contract drift a test catches in seconds.
This module validates every template placeholder against the producing
node's declared output contract at test time and fails naming the missing
field + the available ones.

Layers:
- structural (all templates, hard fail): `${param.f}` must exist in the
  template's declared params; `${step.<nid>...}` must reference an existing
  node; placeholder paths must be non-empty; `${run_id}` always valid
- typed (when the producing node+op has a declared contract, hard fail):
  first path segment of `${result.<seg>...}` / `${step.<nid>.<seg>...}` must
  be a declared top-level output key or a declared `result`-envelope key;
  the error lists the available fields
- undeclared node/op combinations pass structural validation only —
  contracts grow as nodes are audited (registry below is the seed)

The n12-class catch: rename a producer field without updating templates →
CI fails with the named missing field.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Producer output contracts — keys of NodeResult.data ("top") and, when the
# node wraps its payload in {"result": ...}, the envelope's known keys.
# "free" = envelope is an upstream JSON blob, first segment not statically
# checkable (e.g. raw REST response of generic github_api ops).
# Verify against the node source when editing (grep the NodeResult data= dict).
_PLACEHOLDER_RE = re.compile(r"\$\{\s*(param|result|step|run_id)\s*\.?\s*([^}]*)\}")

NODE_OUTPUT_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "github_api": {
        "top": {
            "result",
            "deduplicated",
            "state",
            "issue_closed",
            "pr_url",
            "pr_number",
            "ci_status",
            "pr_link_strength",
            "found",
            "notification",
        },
        # op → {"result": envelope keys | "free", "extra": extra top-level keys}
        "ops": {
            "linked_pr": {"result": {"found", "pr_url", "pr_number", "ci_status", "pr_link_strength"}},
            "issue_create": {"result": "free", "extra": {"deduplicated", "state"}},
            "issue_comment": {"result": "free"},
            "issue_set_labels": {"result": "free"},
            "pr_get": {"result": "free"},
            "issue_list": {"result": "free"},
        },
    },
    "studio_run": {
        "top": {"run_id", "status", "result", "report", "detached", "recovered_run_id", "recovery"},
    },
    "claude_dispatch": {
        "top": {
            "pr_url",
            "pr_number",
            "outcome",
            "session_claim",
            "notification",
            "total_tokens",
            "detached",
            "pid",
            "output_head",
            "issue_closed",
        },
    },
}


class TemplateContractError(Exception):
    pass


def export_contracts() -> Dict[str, Any]:
    """Snapshot of the contract registry — shared cross-repo fixture file
    (fusion_cowork/templates/contracts.json); the agent-studio companion
    pack consumes the same file. Single source of truth stays here."""
    return {
        "version": 1,
        "consumers": [
            "fusion-cowork (fusion_cowork/templates/contract.py)",
            "agent-studio contract test pack (companion, same fixture path)",
        ],
        "nodes": {
            name: {
                "top": sorted(c.get("top") or ()),
                "ops": {
                    op: {
                        "result": sorted(v) if isinstance(v, set) else v,
                        "extra": sorted(e.get("extra") or ()) if isinstance(e, dict) else [],
                    }
                    for op, e in (c.get("ops") or {}).items()
                    for v in [e.get("result") if isinstance(e, dict) else e]
                },
            }
            for name, c in NODE_OUTPUT_CONTRACTS.items()
        },
    }


def _node_op(node: Dict[str, Any]) -> str:
    params = ((node.get("config") or {}).get("params")) or {}
    if isinstance(params, dict):
        return str(params.get("op") or "")
    return ""


def _upstream_producers(workflow: Dict[str, Any], node_id: str) -> List[str]:
    # source node names feeding this node via edges; edges may be dict or list form
    edges = workflow.get("edges") or []
    names = {}
    for n in workflow.get("nodes") or []:
        if isinstance(n, dict):
            names[n.get("id")] = n.get("name")
    out = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if str(e.get("target_id") or e.get("target") or "") == str(node_id):
            src = str(e.get("source_id") or e.get("source") or "")
            if src in names:
                out.append(str(names[src]))
    return out


def _seg_allowed(seg: str, producer: str, producer_node: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return None if allowed, else an error message naming missing + available."""
    contract = NODE_OUTPUT_CONTRACTS.get(producer)
    if contract is None:
        return None
    top = set(contract.get("top") or ())
    if seg in top:
        return None
    ops = contract.get("ops") or {}
    op = _node_op(producer_node) if producer_node else ""
    entry = ops.get(op)
    if entry is not None:
        env = entry.get("result")
        if env == "free":
            return None
        if isinstance(env, set) and seg in env:
            return None
    extra = sorted((entry or {}).get("extra") or ())
    available = sorted(top | set(extra))
    return f"字段 '{seg}' 不在产出节点 '{producer}' 的输出契约中; 可用字段: {available}"


def validate_template_placeholders(template: Dict[str, Any]) -> List[str]:
    """Validate every ${...} placeholder in a template. Returns error strings ([] = pass)."""
    errors: List[str] = []
    tpl_params = template.get("params") or {}
    param_names = set((tpl_params.get("properties") or {}).keys()) if isinstance(tpl_params, dict) else set()
    workflow = template.get("workflow") or {}
    nodes = {str(n.get("id")): n for n in (workflow.get("nodes") or []) if isinstance(n, dict)}

    def walk(value: Any, where: str, node_id: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{where}.{k}", node_id)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]", node_id)
        elif isinstance(value, str):
            for m in _PLACEHOLDER_RE.finditer(value):
                kind, rest = m.group(1), (m.group(2) or "").strip()
                if kind == "run_id":
                    continue
                if not rest:
                    errors.append(f"{where}: 占位符 ${{{kind}}} 缺少字段路径")
                    continue
                seg = rest.split(".")[0].split("[")[0].strip()
                if kind == "param":
                    if seg not in param_names:
                        errors.append(f"{where}: ${{param.{seg}}} 不在模板参数中; 可用参数: {sorted(param_names)}")
                elif kind == "step":
                    nid = rest.split(".")[0].split("[")[0].strip()
                    if nid not in nodes:
                        errors.append(f"{where}: ${{step.{nid}}} 引用不存在的节点; 已有节点: {sorted(nodes)}")
                        continue
                    sub = rest.split(".", 1)[1] if "." in rest else ""
                    if sub:
                        seg2 = sub.split(".")[0].split("[")[0].strip()
                        producer = str(nodes[nid].get("name") or "")
                        msg = _seg_allowed(seg2, producer, nodes[nid])
                        if msg:
                            errors.append(f"{where}: ${{step.{rest}}} — {msg}")
                elif kind == "result":
                    producers = _upstream_producers(workflow, node_id) if node_id else []
                    if not producers:
                        continue  # fan-in ambiguity or edge-less template — structural layer only
                    checked = []
                    for p in producers:
                        pnode = next((n for n in nodes.values() if n.get("name") == p), None)
                        checked.append(_seg_allowed(seg, p, pnode))
                    if all(c is not None for c in checked):
                        errors.append(f"{where}: ${{result.{seg}}} — {'; '.join(str(c) for c in checked)}")

    for nid, n in nodes.items():
        walk(n.get("config"), f"nodes.{nid}", nid)
    # placeholders may also appear in param defaults (descriptions excluded)
    walk(tpl_params.get("properties") if isinstance(tpl_params, dict) else {}, "params")
    return errors


def validate_template_structure(template: Dict[str, Any]) -> List[str]:
    """Node-name resolution: every workflow node name must be a registered node or alias."""
    errors: List[str] = []
    try:
        from fusion_cowork import NODE_NAME_ALIASES
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.nodes import import_all_nodes

        import_all_nodes()
    except Exception as e:
        return [f"节点注册表不可用, 跳过结构校验: {e}"]
    workflow = template.get("workflow") or {}
    aliases = NODE_NAME_ALIASES or {}
    for n in workflow.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name") or "")
        if not name:
            errors.append(f"节点 {n.get('id')} 缺少 name")
            continue
        known = NodeRegistry.get(name) is not None or name in aliases
        if not known:
            errors.append(f"节点 {n.get('id')} 引用未注册节点 '{name}'")
    return errors
