from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONNECTOR_DIR = Path.home() / ".fusion-cowork" / "connectors"
DEFAULT_RAG_WIRED_PATH = Path.home() / ".fusion-cowork" / "web" / "rag_wired.json"
DEFAULT_STUDIO_URL = "http://127.0.0.1:11442"
_STUDIO_TIMEOUT = 3.0
_ROUTE_BY_TYPE = {"skill": "/skills", "connector": "/extensions", "subagent": "/templates"}


def _id_of(kind: str, raw: str) -> str:
    return f"{kind}.{str(raw).strip()}"


def _scope(scope_id: str, sensitive: bool = False) -> Dict[str, Any]:
    return {"id": str(scope_id), "sensitive": bool(sensitive)}


def _instance_state(store: Any, workspace_id: str, cap_type: str, cap_id: str) -> Tuple[bool, bool, Dict[str, Any]]:
    entry = store.get(workspace_id, cap_type, cap_id) if store is not None else None
    if entry is None:
        return False, False, {"usage": {"count": 0, "last_used_at": None}, "granted_scopes": []}
    return (
        entry.enabled,
        True,
        {
            "usage": entry.usage.to_dict(),
            "granted_scopes": list(entry.granted_scopes),
            "connected": bool(entry.credentials),
            "spawn_policy": dict(entry.spawn_policy),
        },
    )


def _finish(item: Dict[str, Any], store: Any, workspace_id: str) -> Dict[str, Any]:
    cap_type = item["type"]
    cap_id = item["id"]
    enabled, installed, state = _instance_state(store, workspace_id, cap_type, cap_id)
    item["installed"] = installed
    item["enabled"] = bool(enabled) if installed else False
    item["usage"] = state["usage"]
    item["granted_scopes"] = state.get("granted_scopes", [])
    item["connected"] = bool(state.get("connected", False))
    entry_policy = state.get("spawn_policy") or {}
    if entry_policy:
        item["spawn_policy"] = entry_policy
    scopes = item.get("scopes") or []
    sensitive = [s["id"] for s in scopes if s.get("sensitive")]
    ungranted = [s for s in sensitive if s not in item["granted_scopes"]]
    item["sensitive_scopes"] = sensitive
    item["requires_approval"] = bool(ungranted)
    if cap_type == "connector":
        auth = item.setdefault("auth", {"kind": "none", "configured": True})
        auth["connected"] = item["connected"]
        auth["requires_approval"] = item["requires_approval"]
    return item


def _skill_items(skills_dir: Optional[Path]) -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    try:
        from ..skills.registry import SkillRegistry

        for skill in SkillRegistry().list_skills():
            cap_id = _id_of("skill", skill.name)
            items[cap_id] = {
                "id": cap_id,
                "type": "skill",
                "name": skill.name,
                "title": skill.name,
                "description": skill.description,
                "source": "builtin",
                "category": skill.category,
                "detail_route": "/skills",
                "detail": {"category": skill.category, "aliases": list(skill.aliases)},
            }
    except Exception as e:
        logger.warning(f"能力目录: 内置技能枚举失败 (跳过): {e}")
    try:
        from ..skills.persistence import list_skill_packs

        for pack in list_skill_packs(skills_dir=skills_dir):
            cap_id = _id_of("skill", pack.name)
            items[cap_id] = {
                "id": cap_id,
                "type": "skill",
                "name": pack.name,
                "title": pack.name,
                "description": pack.description,
                "source": "user_pack",
                "category": pack.category,
                "detail_route": "/extensions",
                "detail": {
                    "handler": pack.type,
                    "node": pack.node,
                    "template_id": pack.template_id,
                    "aliases": list(pack.aliases),
                },
            }
    except Exception as e:
        logger.warning(f"能力目录: JSON skill 包枚举失败 (跳过): {e}")
    try:
        from ..skills import get_package_registry

        for pkg in get_package_registry().list_packages():
            cap_id = _id_of("skill", pkg.name)
            items.setdefault(
                cap_id,
                {
                    "id": cap_id,
                    "type": "skill",
                    "name": pkg.name,
                    "title": pkg.title or pkg.name,
                    "title_en": pkg.title_en,
                    "description": pkg.description,
                    "description_en": pkg.description_en,
                    "source": "markdown_pack",
                    "category": pkg.category,
                    "detail_route": "/skills",
                    "detail": {"handler": pkg.handler, "aliases": list(pkg.aliases)},
                },
            )
    except Exception as e:
        logger.warning(f"能力目录: markdown skill 包枚举失败 (跳过): {e}")
    return sorted(items.values(), key=lambda i: i["id"])


def _load_connector_configs(connector_dir: Optional[Path]) -> List[Dict[str, Any]]:
    root = Path(connector_dir) if connector_dir else DEFAULT_CONNECTOR_DIR
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"连接器配置解析失败 {path}: {e}")
            continue
        if not isinstance(data, dict) or not data.get("id"):
            logger.warning(f"连接器配置缺 id, 跳过 {path}")
            continue
        out.append(data)
    return out


def _rag_wired(rag_wired_path: Optional[Path]) -> List[str]:
    path = Path(rag_wired_path) if rag_wired_path else DEFAULT_RAG_WIRED_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"能力目录: rag wired 状态读取失败 {path}: {e}")
        return []
    wired = data.get("wired") if isinstance(data, dict) else None
    return [str(k) for k in wired] if isinstance(wired, list) else []


def _mcp_registry_items() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    try:
        from fusion_core.mcp_registry import load_registry
    except ImportError:
        return [], {"name": "mcp_registry", "available": False, "detail": "fusion-core 未安装"}
    try:
        records = load_registry()
    except Exception as e:
        logger.warning(f"能力目录: MCP 注册表加载失败: {e}")
        return [], {"name": "mcp_registry", "available": False, "detail": str(e)}
    items = []
    for record in records:
        scopes = [_scope(s, sensitive=False) for s in record.tool_scopes]
        items.append(
            {
                "id": _id_of("connector", record.name),
                "type": "connector",
                "name": record.name,
                "title": record.name,
                "description": record.purpose,
                "source": "mcp_registry",
                "transport": record.transport,
                "status": record.status,
                "detail_route": "/extensions",
                "scopes": scopes,
                "auth": {
                    "kind": "env_ref" if record.credential_mode == "env_ref" else "identity",
                    "configured": record.status == "approved",
                },
                "detail": {
                    "port": record.port,
                    "url_prefixes": list(record.url_prefixes),
                    "credential_mode": record.credential_mode,
                    "notes": record.notes,
                },
            }
        )
    return items, {"name": "mcp_registry", "available": True, "detail": f"{len(items)} 个服务器"}


def _connector_items(
    connector_dir: Optional[Path], rag_wired_path: Optional[Path]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    items: Dict[str, Dict[str, Any]] = {}
    for wired in _rag_wired(rag_wired_path):
        cap_id = _id_of("connector", f"knowledge.{wired}")
        items[cap_id] = {
            "id": cap_id,
            "type": "connector",
            "name": f"knowledge.{wired}",
            "title": f"知识库 {wired}",
            "title_en": f"Knowledge base {wired}",
            "description": "fusion-rag 知识库检索 (本机, 已绑定)",
            "source": "knowledge",
            "detail_route": "/extensions",
            "scopes": [_scope("knowledge.search")],
            "auth": {"kind": "none", "configured": True},
            "detail": {"kb_id": wired, "capability": f"knowledge.{wired}.search"},
        }
    for cfg in _load_connector_configs(connector_dir):
        raw_id = str(cfg.get("id", "")).strip()
        if not raw_id or any(c in raw_id for c in "/\\ "):
            logger.warning(f"连接器 id 非法, 跳过: {raw_id!r}")
            continue
        scopes = []
        for s in cfg.get("scopes") or []:
            if isinstance(s, dict) and s.get("id"):
                scopes.append(_scope(s["id"], bool(s.get("sensitive", False))))
            elif isinstance(s, str):
                scopes.append(_scope(s))
        oauth = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else None
        auth_kind = "oauth" if oauth else ("env_ref" if cfg.get("credential_env") else "none")
        configured = True
        if oauth:
            missing = [key for key in ("authorize_url", "token_url", "client_id_env") if not oauth.get(key)]
            if missing:
                configured = False
                logger.warning(f"连接器 {raw_id} OAuth 元数据缺字段: {missing}")
            else:
                configured = bool(os.environ.get(str(oauth["client_id_env"]), "").strip())
        items[_id_of("connector", raw_id)] = {
            "id": _id_of("connector", raw_id),
            "type": "connector",
            "name": raw_id,
            "title": str(cfg.get("name") or raw_id),
            "title_en": str(cfg.get("name_en") or cfg.get("name") or raw_id),
            "description": str(cfg.get("description") or ""),
            "source": "connector_config",
            "transport": str(cfg.get("transport") or "mcp"),
            "url": str(cfg.get("url") or ""),
            "detail_route": "/extensions",
            "scopes": scopes,
            "auth": {"kind": auth_kind, "configured": configured},
            "detail": {
                "url": str(cfg.get("url") or ""),
                "oauth": {
                    "authorize_url": (oauth or {}).get("authorize_url", ""),
                    "token_url": (oauth or {}).get("token_url", ""),
                    "client_id_env": (oauth or {}).get("client_id_env", ""),
                    "scopes": list((oauth or {}).get("scopes") or []),
                }
                if oauth
                else None,
            },
        }
    registry_items, registry_source = _mcp_registry_items()
    for item in registry_items:
        items.setdefault(item["id"], item)
    return sorted(items.values(), key=lambda i: i["id"]), [registry_source]


def _template_items() -> List[Dict[str, Any]]:
    try:
        from ..templates import TemplateManager

        templates = TemplateManager().list_templates()
    except Exception as e:
        logger.warning(f"能力目录: 模板枚举失败 (跳过): {e}")
        return []
    items = []
    for tpl in templates:
        tpl_id = str(tpl.get("id", ""))
        if not tpl_id:
            continue
        wf = tpl.get("workflow") if isinstance(tpl.get("workflow"), dict) else {}
        nodes = wf.get("nodes") if isinstance(wf.get("nodes"), list) else []
        items.append(
            {
                "id": _id_of("subagent", f"template.{tpl_id}"),
                "type": "subagent",
                "name": f"template.{tpl_id}",
                "title": str(tpl.get("name") or tpl_id),
                "title_en": str(tpl.get("name_en") or tpl.get("name") or tpl_id),
                "description": str(tpl.get("description") or ""),
                "source": "dag_template",
                "detail_route": "/templates",
                "scopes": [],
                "spawn_policy": {"mode": "sync", "requires_approval": False, "template_id": tpl_id},
                "detail": {
                    "kind": "template",
                    "template_id": tpl_id,
                    "node_count": len(nodes),
                    "node_names": [n.get("name") for n in nodes if isinstance(n, dict)][:20],
                },
            }
        )
    return items


async def _studio_items(
    studio_base_url: Optional[str], studio_api_key: Optional[str], transport: Any
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    import httpx

    base = (studio_base_url or os.environ.get("FUSION_AGENT_STUDIO_URL", DEFAULT_STUDIO_URL)).rstrip("/")
    key = (studio_api_key or os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "")).strip()
    headers = {"x-api-key": key} if key else {}
    items: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            base_url=base, headers=headers, timeout=_STUDIO_TIMEOUT, transport=transport
        ) as client:
            graphs: List[Dict[str, Any]] = []
            agents: List[Dict[str, Any]] = []
            errors: List[str] = []
            try:
                resp = await client.get("/v1/graphs", params={"page": 1, "limit": 50})
                resp.raise_for_status()
                payload = resp.json()
                graphs = payload.get("data") if isinstance(payload, dict) else None
                graphs = graphs if isinstance(graphs, list) else []
            except Exception as e:
                errors.append(f"graphs: {e}")
                logger.warning(f"能力目录: agent-studio graphs 列表失败: {e}")
            try:
                resp = await client.get("/v1/agents", params={"page": 1, "limit": 50})
                resp.raise_for_status()
                payload = resp.json()
                agents = payload.get("data") if isinstance(payload, dict) else None
                agents = agents if isinstance(agents, list) else []
            except Exception as e:
                errors.append(f"agents: {e}")
                logger.warning(f"能力目录: agent-studio agents 列表失败: {e}")
    except Exception as e:
        logger.warning(f"能力目录: agent-studio 不可达 {base}: {e}")
        return [], {"name": "agent_studio", "available": False, "detail": str(e)}
    for g in graphs:
        if not isinstance(g, dict) or not g.get("graph_id"):
            continue
        graph_data = g.get("graph_data") if isinstance(g.get("graph_data"), dict) else {}
        nodes = graph_data.get("nodes") if isinstance(graph_data.get("nodes"), list) else []
        items.append(
            {
                "id": _id_of("subagent", f"graph.{g['graph_id']}"),
                "type": "subagent",
                "name": f"graph.{g['graph_id']}",
                "title": str(g.get("name") or g["graph_id"]),
                "description": str(g.get("description") or ""),
                "source": "agent_studio_graph",
                "detail_route": "/runs",
                "scopes": [],
                "spawn_policy": {"mode": "sync", "requires_approval": True, "timeout_s": 120},
                "detail": {
                    "kind": "graph",
                    "graph_id": g["graph_id"],
                    "node_count": len(nodes),
                    "node_names": [n.get("name") for n in nodes if isinstance(n, dict)][:20],
                },
            }
        )
    for a in agents:
        if not isinstance(a, dict) or not a.get("agent_id"):
            continue
        items.append(
            {
                "id": _id_of("subagent", f"agent.{a['agent_id']}"),
                "type": "subagent",
                "name": f"agent.{a['agent_id']}",
                "title": str(a.get("name") or a["agent_id"]),
                "description": str(a.get("description") or ""),
                "source": "agent_studio_agent",
                "detail_route": "/runs",
                "scopes": [],
                "spawn_policy": {"mode": "async", "requires_approval": True, "timeout_s": 0},
                "detail": {
                    "kind": "agent",
                    "agent_id": a["agent_id"],
                    "status": str(a.get("status") or ""),
                },
            }
        )
    available = not errors
    return items, {
        "name": "agent_studio",
        "available": available,
        "detail": "; ".join(errors) if errors else f"{len(graphs)} graphs / {len(agents)} agents",
    }


async def build_catalog(
    workspace_id: str,
    *,
    store: Any,
    skills_dir: Optional[Path] = None,
    connector_dir: Optional[Path] = None,
    rag_wired_path: Optional[Path] = None,
    studio_base_url: Optional[str] = None,
    studio_api_key: Optional[str] = None,
    transport: Any = None,
    types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    wanted = set(types) if types else {"skill", "connector", "subagent"}
    skills = _skill_items(skills_dir) if "skill" in wanted else []
    connectors: List[Dict[str, Any]] = []
    connector_sources: List[Dict[str, Any]] = []
    if "connector" in wanted:
        connectors, connector_sources = _connector_items(connector_dir, rag_wired_path)
    subagents: List[Dict[str, Any]] = []
    studio_source: Dict[str, Any] = {"name": "agent_studio", "available": False, "detail": "未查询"}
    if "subagent" in wanted:
        subagents = _template_items()
        studio_items, studio_source = await _studio_items(studio_base_url, studio_api_key, transport)
        subagents.extend(studio_items)
    sources = list(connector_sources) + [studio_source]
    groups = {"skill": skills, "connector": connectors, "subagent": subagents}
    for cap_type, items in groups.items():
        for item in items:
            _finish(item, store, workspace_id)
            item.setdefault("detail_route", _ROUTE_BY_TYPE.get(cap_type, "/capabilities"))
    logger.info(
        f"能力目录: ws={workspace_id} skills={len(skills)} connectors={len(connectors)} subagents={len(subagents)}"
    )
    return {
        "workspace_id": workspace_id,
        "skills": skills,
        "connectors": connectors,
        "subagents": subagents,
        "sources": sources,
        "count": len(skills) + len(connectors) + len(subagents),
    }


def load_connector_config(connector_dir: Optional[Path], connector_name: str) -> Optional[Dict[str, Any]]:
    for cfg in _load_connector_configs(connector_dir):
        if str(cfg.get("id", "")).strip() == connector_name:
            return cfg
    return None


def search_items(
    query: str,
    workspace_id: str,
    *,
    store: Any,
    skills_dir: Optional[Path] = None,
    connector_dir: Optional[Path] = None,
    rag_wired_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    items = _skill_items(skills_dir) + _template_items()
    connectors, _ = _connector_items(connector_dir, rag_wired_path)
    items.extend(connectors)
    hits = []
    for item in items:
        haystack = " ".join(
            str(item.get(k) or "") for k in ("name", "title", "title_en", "description", "description_en")
        ).lower()
        if q in haystack:
            hits.append(_finish(item, store, workspace_id))
    return hits


def build_connector_map(
    *, connector_dir: Optional[Path] = None, rag_wired_path: Optional[Path] = None
) -> Dict[str, Dict[str, Any]]:
    connectors, _ = _connector_items(connector_dir, rag_wired_path)
    out: Dict[str, Dict[str, Any]] = {}
    for item in connectors:
        detail = item.get("detail") or {}
        url = str(item.get("url") or "")
        prefixes = [str(p).rstrip("/") for p in detail.get("url_prefixes") or [] if p]
        name = str(item.get("name") or "")
        out[item["id"]] = {
            "id": item["id"],
            "name": name,
            "url": url.rstrip("/"),
            "url_prefixes": prefixes,
            "scopes": list(item.get("scopes") or []),
            "auth": dict(item.get("auth") or {}),
            "source": item.get("source", ""),
        }
    return out
