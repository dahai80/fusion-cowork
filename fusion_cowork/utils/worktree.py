"""Git Worktree 隔离管理 — P2-1。

为工作流执行提供 git 工作树隔离: 在独立 worktree 中运行命令/节点,
不污染主工作区。封装 git worktree add/remove/list + 在 worktree 内执行命令。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
    """管理 git worktree 隔离工作树。"""

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
    ) -> Optional[Worktree]:
        """创建 worktree。branch 空 → 新建分支 worktree/<name>; 否则 checkout 已有分支。"""
        if not self.is_git_repo():
            logger.error(f"非 git 仓库, 无法创建 worktree: {self.repo_root}")
            return None
        parent_dir = Path(parent) if parent else self.repo_root.parent / ".fusion-worktrees"
        parent_dir.mkdir(parents=True, exist_ok=True)
        wt_path = parent_dir / name
        if wt_path.exists():
            logger.error(f"worktree 路径已存在: {wt_path}")
            return None
        new_branch = branch or f"worktree/{name}"
        if not branch:
            args = ["worktree", "add", "-b", new_branch, str(wt_path), base_ref]
        else:
            args = ["worktree", "add", str(wt_path), branch]
        res = self._git(args)
        if res.returncode != 0:
            logger.error(f"git worktree add 失败: {res.stderr.strip()}")
            return None
        head = self._git(["rev-parse", "HEAD"], cwd=wt_path).stdout.strip()
        import time

        wt = Worktree(
            path=str(wt_path),
            branch=branch or new_branch,
            repo_root=str(self.repo_root),
            head=head,
            created_at=time.time(),
        )
        self._worktrees[name] = wt
        logger.info(f"worktree 已创建: {name} -> {wt_path} (branch={wt.branch})")
        return wt

    def remove(self, name: str, force: bool = False) -> bool:
        wt = self._worktrees.get(name)
        if not wt:
            logger.warning(f"worktree 未登记: {name}")
            return False
        args = ["worktree", "remove", wt.path]
        if force:
            args.append("--force")
        res = self._git(args)
        if res.returncode != 0:
            logger.warning(f"git worktree remove 失败: {res.stderr.strip()}, 尝试强制删除目录")
            try:
                shutil.rmtree(wt.path, ignore_errors=True)
            except Exception:
                pass
        if wt.branch.startswith("worktree/"):
            self._git(["branch", "-D", wt.branch])
        del self._worktrees[name]
        logger.info(f"worktree 已删除: {name}")
        return True

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
        """在指定 worktree 内执行 shell 命令。"""
        wt = self._worktrees.get(name)
        if not wt:
            return {"error": f"worktree 不存在: {name}"}
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
                return {"error": f"执行超时 ({timeout}s)", "command": command}
            return {
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                "return_code": proc.returncode,
                "worktree": name,
                "path": wt.path,
            }
        except Exception as e:
            logger.error(f"worktree exec 失败 {name}: {e}")
            return {"error": str(e)}
