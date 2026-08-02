"""AST Diff 传输 — 仅传输 AST 变更部分，减少网络开销。

迁移自 fusion-multi-node (AR审计 P2)，原模块已标记 DeprecationWarning。
- compute_ast_diff: 计算 old_ast 与 new_ast 之间的差异
- apply_ast_diff: 从 base_ast + diff 重建完整 AST
- 差异类型: added_nodes, removed_nodes, modified_nodes
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _collect_nodes(tree: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    node_id = tree.get("id", "")
    path = f"{prefix}/{node_id}" if prefix else node_id

    if node_id:
        nodes[path] = {
            "id": node_id,
            "type": tree.get("type", ""),
            "value": tree.get("value"),
            "children_count": len(tree.get("children", [])),
        }

    for child in tree.get("children", []):
        child_nodes = _collect_nodes(child, path)
        nodes.update(child_nodes)

    return nodes


def compute_ast_diff(old_ast: dict[str, Any], new_ast: dict[str, Any]) -> dict[str, Any]:
    old_nodes = _collect_nodes(old_ast)
    new_nodes = _collect_nodes(new_ast)

    old_paths: set[str] = set(old_nodes.keys())
    new_paths: set[str] = set(new_nodes.keys())

    added_paths = new_paths - old_paths
    removed_paths = old_paths - new_paths
    common_paths = old_paths & new_paths

    added_nodes: list[dict[str, Any]] = []
    for p in sorted(added_paths):
        added_nodes.append({"path": p, **new_nodes[p]})

    removed_nodes: list[str] = sorted(removed_paths)

    modified_nodes: list[dict[str, Any]] = []
    for p in sorted(common_paths):
        old_n = old_nodes[p]
        new_n = new_nodes[p]
        if old_n != new_n:
            diff_entry: dict[str, Any] = {"path": p}
            for key in new_n:
                if old_n.get(key) != new_n.get(key):
                    diff_entry[key] = new_n[key]
            modified_nodes.append(diff_entry)

    diff = {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "stats": {
            "added": len(added_nodes),
            "removed": len(removed_nodes),
            "modified": len(modified_nodes),
        },
    }

    logger.info(f"AST diff computed: +{len(added_nodes)} -{len(removed_nodes)} ~{len(modified_nodes)}")
    return diff


def apply_ast_diff(base_ast: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base_ast)

    removed_paths = set(diff.get("removed_nodes", []))
    _remove_nodes(result, removed_paths)

    for mod in diff.get("modified_nodes", []):
        path = mod["path"]
        _update_node(result, path, mod)

    for added in diff.get("added_nodes", []):
        path = added["path"]
        _insert_node(result, path, added)

    stats = diff.get("stats", {})
    logger.info(f"AST diff applied: +{stats.get('added', 0)} -{stats.get('removed', 0)} ~{stats.get('modified', 0)}")
    return result


def _find_node(tree: dict[str, Any], path: str) -> dict[str, Any] | None:
    parts = path.strip("/").split("/")
    if not parts or parts[0] == "":
        return tree

    if parts[0] == tree.get("id"):
        if len(parts) == 1:
            return tree
        parts = parts[1:]

    current = tree
    for part in parts[:-1]:
        found = False
        for child in current.get("children", []):
            if child.get("id") == part:
                current = child
                found = True
                break
        if not found:
            return None

    last = parts[-1]
    for child in current.get("children", []):
        if child.get("id") == last:
            return child
    if current.get("id") == last:
        return current
    return None


def _remove_nodes(tree: dict[str, Any], paths: set[str]) -> None:
    for path in paths:
        parts = path.strip("/").split("/")
        if len(parts) < 1:
            continue
        if parts[0] == tree.get("id") and len(parts) > 1:
            parts = parts[1:]
        current = tree
        for part in parts[:-1]:
            found = False
            for child in current.get("children", []):
                if child.get("id") == part:
                    current = child
                    found = True
                    break
            if not found:
                break

        last_id = parts[-1]
        current["children"] = [c for c in current.get("children", []) if c.get("id") != last_id]


def _update_node(tree: dict[str, Any], path: str, updates: dict[str, Any]) -> None:
    node = _find_node(tree, path)
    if node is None:
        logger.warning(f"AST diff: node not found {path}, skip update")
        return
    for key, value in updates.items():
        if key == "path":
            continue
        node[key] = value


def _insert_node(tree: dict[str, Any], path: str, node_data: dict[str, Any]) -> None:
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        logger.warning(f"AST diff: cannot insert root-level node {path}")
        return

    parent_path = "/".join(parts[:-1])
    parent = _find_node(tree, parent_path)
    if parent is None:
        logger.warning(f"AST diff: parent {parent_path} not found, skip insert {path}")
        return

    new_node: dict[str, Any] = {
        "id": node_data.get("id", parts[-1]),
        "type": node_data.get("type", ""),
        "children": [],
    }
    if "value" in node_data:
        new_node["value"] = node_data["value"]

    parent.setdefault("children", []).append(new_node)
