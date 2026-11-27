"""Git worktree isolation management — P2-1.

Provides git working-tree isolation for workflow execution: run commands/nodes
inside a separate worktree without polluting the main workspace. Wraps git
worktree add/remove/list + command execution inside a worktree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MARKER_FILE = ".fusion-worktree.json"


@dataclass
class Worktree:
    path: str
    branch: str = ""
    repo_root: str = ""
    head: str = ""
    created_at: float = 0.0
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "repo_root": self.repo_root,
            "head": self.head,
            "created_at": self.created_at,
            "tags": self.tags,
        }


class WorktreeManager:
    """Manage git worktree isolated working trees."""

    def __init__(self, repo_root: str = ""):
        self.repo_root = Path(repo_root).resolve() if repo_root else self._detect_repo_root()
        self._worktrees: Dict[str, Worktree] = {}

    @staticmethod
    def _detect_repo_root() -> Path:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
            ).strip()
            return Path(out)
        except Exception:
            return Path.cwd()

    def _git(self, args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

    def is_git_repo(self) -> bool:
        if not self.repo_root.exists():
            return False
        return self._git(["rev-parse", "--is-inside-work-tree"]).returncode == 0

    def create(
        self,
        name: str,
        branch: str = "",
        base_ref: str = "HEAD",
        parent: str = "",
        source_repo: str = "",
        trusted: bool = False,
    ) -> Optional[Worktree]:
        """Create a worktree. Empty branch -> new branch worktree/<name>; otherwise checkout the existing branch.

        issue #580: non-empty source_repo different from this repo's origin -> marked
        untrusted_source; exec is gated by the trust gate (trusted=True or mark_trusted unlocks).
        """
        if not self.is_git_repo():
            logger.error(f"Not a git repository, cannot create worktree: {self.repo_root}")
            return None
        parent_dir = Path(parent) if parent else self.repo_root.parent / ".fusion-worktrees"
        parent_dir.mkdir(parents=True, exist_ok=True)
        wt_path = parent_dir / name
        if wt_path.exists():
            logger.error(f"Worktree path already exists: {wt_path}")
            return None
        new_branch = branch or f"worktree/{name}"
        if not branch:
            args = ["worktree", "add", "-b", new_branch, str(wt_path), base_ref]
        else:
            args = ["worktree", "add", str(wt_path), branch]
        res = self._git(args)
        if res.returncode != 0:
            logger.error(f"git worktree add failed: {res.stderr.strip()}")
            return None
        head = self._git(["rev-parse", "HEAD"], cwd=wt_path).stdout.strip()
        untrusted = ""
        if source_repo:
            own = self._git(["remote", "get-url", "origin"]).stdout.strip()
            if source_repo != own:
                untrusted = source_repo
                logger.warning(
                    f"worktree '{name}' source untrusted (source={source_repo} != origin={own}): "
                    "exec is gated, explicit trust_source required to unlock"
                )
        wt = Worktree(
            path=str(wt_path),
            branch=branch or new_branch,
            repo_root=str(self.repo_root),
            head=head,
            created_at=time.time(),
        )
        self._worktrees[name] = wt
        self._write_marker(wt_path, name=name, branch=wt.branch, untrusted_source=untrusted, trusted=trusted)
        logger.info(f"Worktree created: {name} -> {wt_path} (branch={wt.branch}, untrusted={bool(untrusted)})")
        return wt

    def _write_marker(self, wt_path: Path, name: str, branch: str, untrusted_source: str, trusted: bool) -> None:
        # issue #580: ownership proof — marker file written at creation, cross-checked before delete/merge
        marker = {
            "name": name,
            "branch": branch,
            "repo_root": str(self.repo_root),
            "created_at": time.time(),
            "untrusted_source": untrusted_source,
            "trusted": trusted,
        }
        try:
            tmp = wt_path / (MARKER_FILE + ".tmp")
            tmp.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
            tmp.replace(wt_path / MARKER_FILE)
        except Exception as e:
            logger.error(f"Worktree marker write failed {wt_path}: {e}")

    def read_marker(self, wt_path: str | Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads((Path(wt_path) / MARKER_FILE).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _porcelain_paths(self) -> List[str]:
        res = self._git(["worktree", "list", "--porcelain"])
        if res.returncode != 0:
            return []
        return [line[len("worktree ") :] for line in res.stdout.splitlines() if line.startswith("worktree ")]

    def _resolve(self, name: str) -> Optional[Worktree]:
        # issue #580: resolution does not trust path-tail matching — only in-memory registration or marker name match
        wt = self._worktrees.get(name)
        if wt:
            return wt
        for p in self._porcelain_paths():
            marker = self.read_marker(p)
            if marker and marker.get("name") == name:
                wt = Worktree(path=p, branch=marker.get("branch", ""), repo_root=str(self.repo_root))
                self._worktrees[name] = wt
                return wt
        return None

    def remove(self, name: str, force: bool = False) -> bool:
        # issue #580: ownership-proof delete — marker must exist and be cross-confirmed against
        # git worktree list; directories without a marker are always rejected (path-tail/prefix
        # matching is untrusted), force only forwards git --force
        wt = self._resolve(name)
        if not wt:
            logger.warning(f"Worktree unregistered or lacking ownership proof, refusing to delete: {name}")
            return False
        marker = self.read_marker(wt.path)
        if not marker or marker.get("name") != name:
            logger.warning(f"Worktree marker missing or name mismatch, refusing to delete: {name} ({wt.path})")
            return False
        if wt.path not in self._porcelain_paths():
            logger.warning(f"Worktree not in git worktree list, refusing to delete: {name} ({wt.path})")
            return False
        args = ["worktree", "remove", wt.path]
        if force:
            args.append("--force")
        res = self._git(args)
        if res.returncode != 0:
            logger.warning(f"git worktree remove failed: {res.stderr.strip()}, falling back to force directory removal")
            try:
                shutil.rmtree(wt.path, ignore_errors=True)
            except Exception:
                pass
        if wt.branch.startswith("worktree/"):
            self._git(["branch", "-D", wt.branch])
        self._worktrees.pop(name, None)
        logger.info(f"Worktree deleted: {name}")
        return True

    def is_untrusted(self, name: str) -> Optional[str]:
        # Returns the untrusted source (trusted/unknown -> None; unregistered also None, caller reports separately)
        wt = self._resolve(name)
        if not wt:
            return None
        marker = self.read_marker(wt.path) or {}
        if marker.get("untrusted_source") and not marker.get("trusted"):
            return str(marker["untrusted_source"])
        return None

    def mark_trusted(self, name: str) -> bool:
        wt = self._resolve(name)
        if not wt:
            return False
        marker = self.read_marker(wt.path)
        if not marker:
            return False
        marker["trusted"] = True
        try:
            tmp = Path(wt.path) / (MARKER_FILE + ".tmp")
            tmp.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
            tmp.replace(Path(wt.path) / MARKER_FILE)
        except Exception as e:
            logger.error(f"Worktree marker update failed {wt.path}: {e}")
            return False
        logger.info(f"Worktree source marked trusted: {name} (source={marker.get('untrusted_source')})")
        return True

    def merge_back(
        self,
        name: str,
        target_branch: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        """issue #580: merge the worktree branch back into the baseline — the merge runs in
        the baseline's own working tree (repo_root main worktree), never occupying the agent
        worktree; switches to the target branch and restores the original branch in finally;
        rejects a dirty baseline tree; falls back to merge --abort on failure.
        """
        wt = self._resolve(name)
        if not wt:
            return {"error": f"worktree 不存在: {name}"}  # i18n: user-facing
        marker = self.read_marker(wt.path)
        if not marker or marker.get("name") != name or wt.path not in self._porcelain_paths():
            return {"error": f"worktree 所有权证明缺失, 拒绝合并: {name}"}  # i18n: user-facing
        if self._git(["status", "--porcelain"]).stdout.strip():
            return {"error": "基线工作树有未提交变更, 拒绝合并 (先 commit/stash)"}  # i18n: user-facing
        head_branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        target = target_branch or head_branch
        try:
            if target != head_branch:
                co = self._git(["checkout", target])
                if co.returncode != 0:
                    return {"error": f"切换目标分支失败: {co.stderr.strip()}", "target": target}  # i18n: user-facing
            merge_args = ["merge", "--no-ff", wt.branch]
            if message:
                merge_args += ["-m", message]
            res = self._git(merge_args)
            if res.returncode != 0:
                self._git(["merge", "--abort"])
                logger.warning(f"merge_back failed and rolled back: {name} -> {target}: {res.stderr.strip()}")
                return {"error": f"合并失败 (已 abort): {res.stderr.strip()}", "target": target}  # i18n: user-facing
            commit = self._git(["rev-parse", "HEAD"]).stdout.strip()
            logger.info(f"merge_back done: {name} ({wt.branch}) -> {target} @ {commit[:8]}")
            return {"merged": True, "branch": wt.branch, "target": target, "commit": commit}
        finally:
            if target != head_branch:
                back = self._git(["checkout", head_branch])
                if back.returncode != 0:
                    logger.error(f"Failed to restore original branch: {head_branch}: {back.stderr.strip()}")

    def list_worktrees(self) -> List[Worktree]:
        res = self._git(["worktree", "list", "--porcelain"])
        worktrees: List[Worktree] = []
        if res.returncode != 0:
            return list(self._worktrees.values())
        cur: Optional[Dict[str, str]] = None
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                if cur:
                    worktrees.append(self._porcelain_to_wt(cur))
                cur = {"path": line[len("worktree ") :]}
            elif cur is None:
                continue
            elif line.startswith("HEAD "):
                cur["head"] = line[len("HEAD ") :]
            elif line.startswith("branch "):
                cur["branch"] = line[len("branch ") :]
            elif line == "":
                if cur:
                    worktrees.append(self._porcelain_to_wt(cur))
                    cur = None
        if cur:
            worktrees.append(self._porcelain_to_wt(cur))
        return worktrees

    def _porcelain_to_wt(self, d: Dict[str, str]) -> Worktree:
        return Worktree(
            path=d.get("path", ""),
            branch=d.get("branch", ""),
            repo_root=str(self.repo_root),
            head=d.get("head", ""),
        )

    def get(self, name: str) -> Optional[Worktree]:
        return self._worktrees.get(name)

    async def exec_in(
        self,
        name: str,
        command: str,
        timeout: int = 30,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute a shell command inside the given worktree."""
        wt = self._resolve(name)
        if not wt:
            return {"error": f"worktree 不存在: {name}"}  # i18n: user-facing
        # issue #580: untrusted-source gate — external-source worktree setup/exec is blocked until explicitly trusted
        untrusted = self.is_untrusted(name)
        if untrusted:
            return {
                "error": f"worktree 来源不可信 (source={untrusted}), 拒绝执行; 需显式 trust_source 解锁",  # i18n: user-facing
                "untrusted_source": untrusted,
                "command": command,
            }
        import os

        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=wt.path,
                env=run_env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return {"error": f"执行超时 ({timeout}s)", "command": command}  # i18n: user-facing
            return {
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                "return_code": proc.returncode,
                "worktree": name,
                "path": wt.path,
            }
        except Exception as e:
            logger.error(f"Worktree exec failed {name}: {e}")
            return {"error": str(e)}
