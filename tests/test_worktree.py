from __future__ import annotations

import asyncio
import subprocess

import pytest

from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.utils.worktree import WorktreeManager

import_all_nodes()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_create_exec_remove_worktree(git_repo):
    mgr = WorktreeManager(repo_root=str(git_repo))
    assert mgr.is_git_repo()
    wt = mgr.create("wt-a", parent=str(git_repo.parent / "wt-tmp"))
    assert wt is not None
    assert wt.branch == "worktree/wt-a"
    result = asyncio.run(mgr.exec_in("wt-a", "echo in_wt; pwd", timeout=10))
    assert "error" not in result
    assert "in_wt" in result["stdout"]
    assert "wt-a" in result["path"]
    assert result["return_code"] == 0
    assert mgr.remove("wt-a", force=True) is True
    assert mgr.get("wt-a") is None


def test_list_worktrees(git_repo):
    import pathlib

    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("wt-l1", parent=str(git_repo.parent / "wt-tmp2"))
    wts = mgr.list_worktrees()
    names = [pathlib.Path(w.path).name for w in wts]
    assert "repo" in names
    assert "wt-l1" in names
    mgr.remove("wt-l1", force=True)


def test_worktree_node_flow(git_repo):
    node_c = NodeRegistry.create(
        "worktree", config=NodeConfig(params={"action": "create", "name": "wt-node", "repo_root": str(git_repo)})
    )
    r = asyncio.run(node_c.execute({}))
    assert r.status.value == "success"
    node_e = NodeRegistry.create(
        "worktree",
        config=NodeConfig(
            params={
                "action": "exec",
                "name": "wt-node",
                "command": "echo node_wt",
                "timeout": 10,
                "repo_root": str(git_repo),
            }
        ),
    )
    r2 = asyncio.run(node_e.execute({}))
    assert r2.status.value == "success"
    assert "node_wt" in r2.data.get("stdout", "")
    node_r = NodeRegistry.create(
        "worktree", config=NodeConfig(params={"action": "remove", "name": "wt-node", "repo_root": str(git_repo)})
    )
    r3 = asyncio.run(node_r.execute({}))
    assert r3.status.value == "success"


def test_non_git_repo_rejected(tmp_path):
    nodir = tmp_path / "notgit"
    nodir.mkdir()
    mgr = WorktreeManager(repo_root=str(nodir))
    assert mgr.is_git_repo() is False
    assert mgr.create("wt-x") is None
