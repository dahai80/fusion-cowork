from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catalog import build_connector_map
from .store import CapabilityStore, normalize_workspace

logger = logging.getLogger(__name__)

ALLOW = "allow"
DENY = "deny"
APPROVAL = "approval"


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _node_url(params: Dict[str, Any]) -> str:
    nested = params.get("params") if isinstance(params.get("params"), dict) else {}
    return _str(params.get("url")) or _str(nested.get("url"))


def _node_tool(params: Dict[str, Any]) -> str:
    nested = params.get("params") if isinstance(params.get("params"), dict) else {}
    return _str(params.get("tool")) or _str(nested.get("tool"))


def _node_graph(params: Dict[str, Any]) -> str:
    nested = params.get("params") if isinstance(params.get("params"), dict) else {}
    return _str(params.get("graph_id")) or _str(nested.get("graph_id"))


def match_connector(url: str, connector_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    target = url.rstrip("/")
    if not target:
        return None
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for connector in connector_map.values():
        prefixes = list(connector.get("url_prefixes") or [])
        if connector.get("url"):
            prefixes.append(connector["url"])
        for prefix in prefixes:
            normalized = str(prefix).rstrip("/")
            if normalized and target.startswith(normalized):
                length = len(normalized)
                if best is None or length > best[0]:
                    best = (length, connector)
    return best[1] if best else None


def scope_matches(scope_id: str, tool: str) -> bool:
    if not scope_id or not tool:
        return False
    if scope_id.endswith("*"):
        return tool.startswith(scope_id[:-1])
    return tool == scope_id or tool.startswith(scope_id + ".")


def _sensitive_ungranted(connector: Dict[str, Any], granted: List[str], tool: str) -> List[str]:
    out = []
    for scope in connector.get("scopes") or []:
        scope_id = str(scope.get("id", ""))
        if not scope.get("sensitive") or scope_id in granted:
            continue
        if tool and not scope_matches(scope_id, tool):
            continue
        out.append(scope_id)
    return out


def make_capability_gate(
    store: CapabilityStore,
    *,
    connector_dir: Optional[Path] = None,
    rag_wired_path: Optional[Path] = None,
) -> Callable[[str, Dict[str, Any], str], Any]:
    async def gate(tool_name: str, params: Dict[str, Any], workspace_id: str) -> Tuple[str, str]:
        try:
            return _decide(store, tool_name, params or {}, workspace_id, connector_dir, rag_wired_path)
        except Exception as e:
            logger.warning(f"能力闸门判定异常, 放行 (由既有权限层兜底) {tool_name}: {e}")
            return ALLOW, ""

    return gate


def _decide(
    store: CapabilityStore,
    tool_name: str,
    params: Dict[str, Any],
    workspace_id: str,
    connector_dir: Optional[Path],
    rag_wired_path: Optional[Path],
) -> Tuple[str, str]:
    ws = normalize_workspace(workspace_id)
    if tool_name.startswith("skill."):
        if not store.has_any(ws, "skill"):
            return ALLOW, ""
        entry = store.get(ws, "skill", tool_name)
        if entry is None:
            return DENY, f"技能未安装到本工作区: {tool_name}"
        if not entry.enabled:
            return DENY, f"技能已在工作区停用: {tool_name}"
        return ALLOW, ""
    if tool_name != "mcp_client":
        return ALLOW, ""
    if not store.has_any(ws, "connector"):
        return ALLOW, ""
    connector_map = build_connector_map(connector_dir=connector_dir, rag_wired_path=rag_wired_path)
    connector = match_connector(_node_url(params), connector_map)
    if connector is None:
        return ALLOW, ""
    entry = store.get(ws, "connector", connector["id"])
    if entry is None:
        return DENY, f"连接器未安装到本工作区: {connector['id']}"
    if not entry.enabled:
        return DENY, f"连接器已在工作区停用: {connector['id']}"
    pending = _sensitive_ungranted(connector, entry.granted_scopes, _node_tool(params))
    if pending:
        return APPROVAL, f"敏感 scope 待审批: {connector['id']} {pending}"
    if (connector.get("auth") or {}).get("kind") == "oauth" and not entry.credentials:
        return APPROVAL, f"连接器未完成 OAuth 授权: {connector['id']}"
    return ALLOW, ""


def capability_of_step(
    step: Dict[str, Any], connector_map: Dict[str, Dict[str, Any]]
) -> Optional[Tuple[str, str, str]]:
    node = str(step.get("node_name") or "")
    inputs = step.get("input_data") if isinstance(step.get("input_data"), dict) else {}
    if node.startswith("skill."):
        return "skill", node, ""
    if node == "mcp_client":
        connector = match_connector(_node_url(inputs), connector_map)
        if connector is None:
            return None
        return "connector", connector["id"], _node_tool(inputs)
    if node == "studio_run":
        graph = _node_graph(inputs)
        if not graph:
            return None
        return "subagent", f"subagent.graph.{graph}", graph
    return None


def record_run_usage(
    store: CapabilityStore,
    record: Dict[str, Any],
    *,
    connector_dir: Optional[Path] = None,
    rag_wired_path: Optional[Path] = None,
) -> int:
    ws = normalize_workspace(record.get("space_id"))
    steps = record.get("steps") if isinstance(record.get("steps"), list) else []
    if not steps:
        return 0
    connector_map = build_connector_map(connector_dir=connector_dir, rag_wired_path=rag_wired_path)
    run_id = str(record.get("run_id") or "")
    workflow = str(record.get("workflow_name") or "")
    actor = str((record.get("labels") or {}).get("actor") or "")
    written = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        resolved = capability_of_step(step, connector_map)
        if resolved is None:
            continue
        cap_type, cap_id, detail = resolved
        status = str(step.get("status") or "")
        verdict = "denied" if status in ("denied", "gated") else status
        ok = store.record_usage(
            ws,
            cap_type,
            cap_id,
            run_id=run_id,
            workflow=workflow,
            node=str(step.get("node_name") or ""),
            detail=detail,
            verdict=verdict,
            actor=actor,
        )
        if ok:
            written += 1
    if written:
        logger.info(f"能力用量入账: ws={ws} run={run_id} entries={written}")
    return written
