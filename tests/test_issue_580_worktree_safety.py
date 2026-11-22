# issue #580: worktree isolation safety pieces (design inputs from paseo analysis)
#
# - ownership-proof deletion: remove requires the .fusion-worktree.json marker
#   AND a git worktree list --porcelain cross-check; path-tail matching removed
# - untrusted-source gate: worktrees created from a foreign source repo are
#   marked untrusted_source in the marker; exec is blocked until explicitly
#   trusted (trust_source param / mark_trusted)
# - merge-back never occupies the agent worktree: merge runs in the base repo's
#   main worktree, target branch checked out and original branch restored in
#   finally, dirty base refused, failed merge aborted

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.utils.worktree import MARKER_FILE, WorktreeManager

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


def _make_wt(git_repo, name="wt-s", **kwargs):
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create(name, parent=str(git_repo.parent / "wt-tmp"), **kwargs)
    assert wt is not None
    return mgr, wt


class TestOwnershipProof:
    def test_marker_written_on_create(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        marker = mgr.read_marker(wt.path)
        assert marker is not None
        assert marker["name"] == "wt-s"
        assert marker["branch"] == "worktree/wt-s"
        mgr.remove("wt-s", force=True)

    def test_remove_without_marker_refused(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        # strip the marker — deletion must now be refused even though the
        # directory is a registered worktree (ownership proof gone)
        (Path(wt.path) / MARKER_FILE).unlink()
        assert mgr.remove("wt-s", force=True) is False
        assert (
            wt.path
            in subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=git_repo, capture_output=True, text=True
            ).stdout
        ), "unowned worktree must survive"
        # restore marker for cleanup
        (Path(wt.path) / MARKER_FILE).write_text(
            json.dumps({"name": "wt-s", "branch": wt.branch, "repo_root": str(git_repo)}), encoding="utf-8"
        )
        mgr.remove("wt-s", force=True)

    def test_remove_unknown_name_refused(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        assert mgr.remove("ghost", force=True) is False
        mgr.remove("wt-s", force=True)

    def test_remove_marker_name_mismatch_refused(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        marker = mgr.read_marker(wt.path)
        marker["name"] = "other"
        (Path(wt.path) / MARKER_FILE).write_text(json.dumps(marker), encoding="utf-8")
        assert mgr.remove("wt-s", force=True) is False
        (Path(wt.path) / MARKER_FILE).write_text(json.dumps({**marker, "name": "wt-s"}), encoding="utf-8")
        mgr.remove("wt-s", force=True)

    def test_node_remove_no_path_tail_fallback(self, git_repo):
        # old behavior: path-tail match let remove/exec resolve unregistered
        # worktrees by directory basename — must be gone
        _make_wt(git_repo, "wt-tail")
        node = NodeRegistry.create(
            "worktree",
            config=NodeConfig(params={"action": "remove", "name": "wt-tail", "repo_root": str(git_repo)}),
        )
        # fresh manager has no in-memory registration; resolution must go
        # through the marker (which exists) — remove succeeds via marker path
        r = asyncio.run(node.execute({}))
        assert r.status.value == "success"
        assert r.data["removed"] is True


class TestUntrustedGate:
    def test_foreign_source_blocked(self, git_repo):
        mgr, wt = _make_wt(git_repo, source_repo="https://github.com/evil/fork.git")
        assert mgr.is_untrusted("wt-s") == "https://github.com/evil/fork.git"
        result = asyncio.run(mgr.exec_in("wt-s", "echo pwn", timeout=10))
        assert "error" in result
        assert "不可信" in result["error"]
        assert "pwn" not in result.get("stdout", "")
        mgr.remove("wt-s", force=True)

    def test_own_origin_not_untrusted(self, git_repo):
        own = subprocess.run(["git", "remote", "get-url", "origin"], cwd=git_repo, capture_output=True, text=True)
        if own.returncode != 0:
            own_url = "file://" + str(git_repo)
            subprocess.run(["git", "remote", "add", "origin", own_url], cwd=git_repo, check=True)
        else:
            own_url = own.stdout.strip()
        mgr, wt = _make_wt(git_repo, source_repo=own_url)
        assert mgr.is_untrusted("wt-s") is None
        result = asyncio.run(mgr.exec_in("wt-s", "echo ok", timeout=10))
        assert "error" not in result
        mgr.remove("wt-s", force=True)

    def test_mark_trusted_unlocks_exec(self, git_repo):
        mgr, wt = _make_wt(git_repo, source_repo="https://github.com/evil/fork.git")
        assert mgr.mark_trusted("wt-s") is True
        assert mgr.is_untrusted("wt-s") is None
        result = asyncio.run(mgr.exec_in("wt-s", "echo trusted_ok", timeout=10))
        assert "error" not in result
        assert "trusted_ok" in result["stdout"]
        mgr.remove("wt-s", force=True)

    def test_trust_source_param_unlocks_at_exec(self, git_repo):
        mgr, wt = _make_wt(git_repo, source_repo="https://github.com/evil/fork.git")
        node = NodeRegistry.create(
            "worktree",
            config=NodeConfig(
                params={
                    "action": "exec",
                    "name": "wt-s",
                    "command": "echo gate_ok",
                    "timeout": 10,
                    "repo_root": str(git_repo),
                    "trust_source": True,
                }
            ),
        )
        r = asyncio.run(node.execute({}))
        assert r.status.value == "success"
        assert "gate_ok" in r.data["stdout"]
        mgr.remove("wt-s", force=True)

    def test_node_exec_blocked_without_trust(self, git_repo):
        mgr, wt = _make_wt(git_repo, source_repo="https://github.com/evil/fork.git")
        node = NodeRegistry.create(
            "worktree",
            config=NodeConfig(
                params={
                    "action": "exec",
                    "name": "wt-s",
                    "command": "echo pwn",
                    "timeout": 10,
                    "repo_root": str(git_repo),
                }
            ),
        )
        r = asyncio.run(node.execute({}))
        assert r.status.value == "failed"
        assert "不可信" in r.error
        mgr.remove("wt-s", force=True)


class TestMergeBack:
    def test_merge_back_into_current_branch(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        (Path(wt.path) / "feature.txt").write_text("feat\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt.path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=wt.path, check=True)
        result = mgr.merge_back("wt-s", message="merge feature")
        assert "error" not in result, result
        assert result["merged"] is True
        assert result["target"] == "master" or result["target"] in ("main", "master")
        assert (git_repo / "feature.txt").exists(), "feature file merged into base"
        # base still on original branch
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        assert head == result["target"]
        mgr.remove("wt-s", force=True)

    def test_merge_back_target_branch_restores_original(self, git_repo):
        subprocess.run(["git", "branch", "release"], cwd=git_repo, check=True)
        mgr, wt = _make_wt(git_repo)
        (Path(wt.path) / "rel.txt").write_text("rel\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt.path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "rel"], cwd=wt.path, check=True)
        result = mgr.merge_back("wt-s", target_branch="release")
        assert "error" not in result, result
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        assert head != "release", "original branch must be restored after merge"
        # rel.txt landed on release, not on the original branch
        rel_file = subprocess.run(
            ["git", "ls-tree", "release", "--name-only"], cwd=git_repo, capture_output=True, text=True
        ).stdout
        assert "rel.txt" in rel_file
        mgr.remove("wt-s", force=True)

    def test_merge_back_dirty_base_refused(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        (Path(wt.path) / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt.path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=wt.path, check=True)
        (git_repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        result = mgr.merge_back("wt-s")
        assert "error" in result
        assert "未提交" in result["error"]
        mgr.remove("wt-s", force=True)

    def test_merge_back_unowned_refused(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        (Path(wt.path) / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt.path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=wt.path, check=True)
        (Path(wt.path) / MARKER_FILE).unlink()
        result = mgr.merge_back("wt-s")
        assert "error" in result
        assert "所有权" in result["error"]
        (Path(wt.path) / MARKER_FILE).write_text(
            json.dumps({"name": "wt-s", "branch": wt.branch, "repo_root": str(git_repo)}), encoding="utf-8"
        )
        mgr.remove("wt-s", force=True)

    def test_node_merge_back_action(self, git_repo):
        mgr, wt = _make_wt(git_repo)
        (Path(wt.path) / "nf.txt").write_text("nf\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt.path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "nf"], cwd=wt.path, check=True)
        node = NodeRegistry.create(
            "worktree",
            config=NodeConfig(
                params={"action": "merge_back", "name": "wt-s", "repo_root": str(git_repo), "message": "via node"}
            ),
        )
        r = asyncio.run(node.execute({}))
        assert r.status.value == "success", r.error
        assert r.data["merged"] is True
        assert (git_repo / "nf.txt").exists()
        mgr.remove("wt-s", force=True)


def test_worktree_high_risk():
    # issue #580: worktree runs arbitrary commands (exec) and deletes trees —
    # same exposure class as shell_exec, must be explicitly approved
    assert "worktree" in HIGH_RISK_NODES
