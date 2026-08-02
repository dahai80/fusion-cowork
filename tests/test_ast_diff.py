"""AST Diff 模块单元测试 — 迁移自 fusion-multi-node。"""

from __future__ import annotations

import copy

from fusion_cowork.engine.ast_diff import (
    _collect_nodes,
    _find_node,
    _insert_node,
    _remove_nodes,
    _update_node,
    apply_ast_diff,
    compute_ast_diff,
)

# ── 测试 AST 树 ──

SAMPLE_OLD_AST = {
    "id": "root",
    "type": "module",
    "children": [
        {
            "id": "func_a",
            "type": "function",
            "value": "def func_a(): pass",
            "children": [
                {"id": "stmt_1", "type": "pass", "children": []},
            ],
        },
        {
            "id": "func_b",
            "type": "function",
            "value": "def func_b(): return 1",
            "children": [],
        },
    ],
}

SAMPLE_NEW_AST = {
    "id": "root",
    "type": "module",
    "children": [
        {
            "id": "func_a",
            "type": "function",
            "value": "def func_a(): return 42",
            "children": [
                {"id": "stmt_1", "type": "return", "children": []},
                {"id": "stmt_2", "type": "assign", "value": "x = 1", "children": []},
            ],
        },
        {
            "id": "func_c",
            "type": "function",
            "value": "def func_c(): pass",
            "children": [],
        },
    ],
}


class TestCollectNodes:
    def test_empty_tree(self):
        result = _collect_nodes({})
        assert result == {}

    def test_single_node(self):
        tree = {"id": "root", "type": "module", "children": []}
        result = _collect_nodes(tree)
        assert "root" in result
        assert result["root"]["type"] == "module"
        assert result["root"]["children_count"] == 0

    def test_nested_tree(self):
        result = _collect_nodes(SAMPLE_OLD_AST)
        assert "root" in result
        assert "root/func_a" in result
        assert "root/func_a/stmt_1" in result
        assert "root/func_b" in result
        assert result["root/func_a"]["children_count"] == 1
        assert result["root/func_b"]["children_count"] == 0


class TestComputeAstDiff:
    def test_identical_asts(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, copy.deepcopy(SAMPLE_OLD_AST))
        assert diff["added_nodes"] == []
        assert diff["removed_nodes"] == []
        assert diff["modified_nodes"] == []
        assert diff["stats"]["added"] == 0
        assert diff["stats"]["removed"] == 0
        assert diff["stats"]["modified"] == 0

    def test_added_nodes(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        added_paths = [n["path"] for n in diff["added_nodes"]]
        assert "root/func_c" in added_paths
        assert "root/func_a/stmt_2" in added_paths

    def test_removed_nodes(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        assert "root/func_b" in diff["removed_nodes"]

    def test_modified_nodes(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        modified_paths = [n["path"] for n in diff["modified_nodes"]]
        assert "root/func_a/stmt_1" in modified_paths
        stmt_1_mod = next(n for n in diff["modified_nodes"] if n["path"] == "root/func_a/stmt_1")
        assert stmt_1_mod["type"] == "return"

    def test_stats(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        assert diff["stats"]["added"] == len(diff["added_nodes"])
        assert diff["stats"]["removed"] == len(diff["removed_nodes"])
        assert diff["stats"]["modified"] == len(diff["modified_nodes"])

    def test_empty_asts(self):
        diff = compute_ast_diff({}, {})
        assert diff["added_nodes"] == []
        assert diff["removed_nodes"] == []
        assert diff["modified_nodes"] == []


class TestFindNode:
    def test_find_root(self):
        node = _find_node(SAMPLE_OLD_AST, "root")
        assert node is not None
        assert node["id"] == "root"

    def test_find_child(self):
        node = _find_node(SAMPLE_OLD_AST, "root/func_a")
        assert node is not None
        assert node["id"] == "func_a"

    def test_find_deep_child(self):
        node = _find_node(SAMPLE_OLD_AST, "root/func_a/stmt_1")
        assert node is not None
        assert node["id"] == "stmt_1"

    def test_not_found(self):
        node = _find_node(SAMPLE_OLD_AST, "root/nonexistent")
        assert node is None

    def test_empty_path(self):
        node = _find_node(SAMPLE_OLD_AST, "")
        assert node is SAMPLE_OLD_AST


class TestRemoveNodes:
    def test_remove_leaf(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _remove_nodes(tree, {"root/func_a/stmt_1"})
        assert len(tree["children"][0]["children"]) == 0

    def test_remove_branch(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _remove_nodes(tree, {"root/func_b"})
        assert len(tree["children"]) == 1
        assert tree["children"][0]["id"] == "func_a"


class TestUpdateNode:
    def test_update_value(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _update_node(tree, "root/func_a", {"path": "root/func_a", "value": "new_value"})
        assert tree["children"][0]["value"] == "new_value"

    def test_update_type(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _update_node(tree, "root/func_a/stmt_1", {"path": "root/func_a/stmt_1", "type": "return"})
        assert tree["children"][0]["children"][0]["type"] == "return"

    def test_update_nonexistent(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _update_node(tree, "root/nonexistent", {"path": "root/nonexistent", "value": "x"})
        assert tree == SAMPLE_OLD_AST


class TestInsertNode:
    def test_insert_child(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _insert_node(
            tree,
            "root/func_a/stmt_new",
            {
                "path": "root/func_a/stmt_new",
                "id": "stmt_new",
                "type": "assign",
                "value": "x = 1",
            },
        )
        func_a = tree["children"][0]
        assert len(func_a["children"]) == 2
        assert func_a["children"][1]["id"] == "stmt_new"

    def test_insert_branch(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _insert_node(
            tree,
            "root/func_c",
            {
                "path": "root/func_c",
                "id": "func_c",
                "type": "function",
            },
        )
        assert len(tree["children"]) == 3
        assert tree["children"][2]["id"] == "func_c"

    def test_insert_root_level_skipped(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _insert_node(
            tree,
            "orphan",
            {
                "path": "orphan",
                "id": "orphan",
                "type": "module",
            },
        )
        assert len(tree["children"]) == 2

    def test_insert_missing_parent_skipped(self):
        tree = copy.deepcopy(SAMPLE_OLD_AST)
        _insert_node(
            tree,
            "root/ghost/child",
            {
                "path": "root/ghost/child",
                "id": "child",
                "type": "expr",
            },
        )
        assert len(tree["children"]) == 2


class TestApplyAstDiff:
    def test_roundtrip(self):
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        result = apply_ast_diff(SAMPLE_OLD_AST, diff)
        assert result["children"][0]["value"] == "def func_a(): return 42"
        func_c_found = any(c["id"] == "func_c" for c in result["children"])
        assert func_c_found
        func_b_found = any(c["id"] == "func_b" for c in result["children"])
        assert not func_b_found

    def test_empty_diff(self):
        diff = {
            "added_nodes": [],
            "removed_nodes": [],
            "modified_nodes": [],
            "stats": {"added": 0, "removed": 0, "modified": 0},
        }
        result = apply_ast_diff(SAMPLE_OLD_AST, diff)
        assert result == SAMPLE_OLD_AST

    def test_does_not_mutate_base(self):
        original = copy.deepcopy(SAMPLE_OLD_AST)
        diff = compute_ast_diff(SAMPLE_OLD_AST, SAMPLE_NEW_AST)
        apply_ast_diff(SAMPLE_OLD_AST, diff)
        assert original == SAMPLE_OLD_AST
