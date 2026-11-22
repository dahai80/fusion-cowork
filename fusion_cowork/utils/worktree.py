"""Git Worktree 隔离管理 — P2-1。

为工作流执行提供 git 工作树隔离: 在独立 worktree 中运行命令/节点,
不污染主工作区。封装 git worktree add/remove/list + 在 worktree 内执行命令。
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
        source_repo: str = "",
        trusted: bool = False,
    ) -> Optional[Worktree]:
        """创建 worktree。branch 空 → 新建分支 worktree/<name>; 否则 checkout 已有分支。

        issue #580: source_repo 非空且 != 本仓 origin → 标记 untrusted_source,
        exec 受信任门禁约束 (trusted=True 或 mark_trusted 解锁)。
        """
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
        untrusted = ""
        if source_repo:
            own = self._git(["remote", "get-url", "origin"]).stdout.strip()
            if source_repo != own:
                untrusted = source_repo
                logger.warning(
                    f"worktree '{name}' 来源不可信 (source={source_repo} != origin={own}): "
                    "exec 被门禁阻止, 需显式 trust_source 解锁"
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
        logger.info(f"worktree 已创建: {name} -> {wt_path} (branch={wt.branch}, untrusted={bool(untrusted)})")
        return wt

    def _write_marker(self, wt_path: Path, name: str, branch: str, untrusted_source: str, trusted: bool) -> None:
        # issue #580: 所有权证明 — 创建时落 marker 文件, 删除/合并前交叉校验
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
            logger.error(f"worktree marker 写入失败 {wt_path}: {e}")

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
        # issue #580: 解析不信任路径尾段匹配 — 只认内存登记或 marker 名称匹配
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
        # issue #580: 所有权证明删除 — 必须 marker 存在且 git worktree list 交叉确认;
        # 无 marker 的目录一律拒绝 (路径尾段/前缀匹配不可信), force 只透传 git --force
        wt = self._resolve(name)
        if not wt:
            logger.warning(f"worktree 未登记或无所有权证明, 拒绝删除: {name}")
            return False
        marker = self.read_marker(wt.path)
        if not marker or marker.get("name") != name:
            logger.warning(f"worktree marker 缺失或名称不符, 拒绝删除: {name} ({wt.path})")
            return False
        if wt.path not in self._porcelain_paths():
            logger.warning(f"worktree 不在 git worktree list 中, 拒绝删除: {name} ({wt.path})")
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
        self._worktrees.pop(name, None)
        logger.info(f"worktree 已删除: {name}")
        return True

    def is_untrusted(self, name: str) -> Optional[str]:
        # 返回 untrusted 来源 (可信/未知 → None; 未登记也 None, 由调用方另行报错)
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
            logger.error(f"worktree marker 更新失败 {wt.path}: {e}")
            return False
        logger.info(f"worktree 来源已信任: {name} (source={marker.get('untrusted_source')})")
        return True

    def merge_back(
        self,
        name: str,
        target_branch: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        """issue #580: 把 worktree 分支合并回基线 — 合并在基线自己的工作树
        (repo_root 主工作树) 执行, 绝不占用 agent worktree; 目标分支切换且
        finally 恢复原分支; 基线脏树拒绝; 失败 merge --abort 兜底。
        """
        wt = self._resolve(name)
        if not wt:
            return {"error": f"worktree 不存在: {name}"}
        marker = self.read_marker(wt.path)
        if not marker or marker.get("name") != name or wt.path not in self._porcelain_paths():
            return {"error": f"worktree 所有权证明缺失, 拒绝合并: {name}"}
        if self._git(["status", "--porcelain"]).stdout.strip():
            return {"error": "基线工作树有未提交变更, 拒绝合并 (先 commit/stash)"}
        head_branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        target = target_branch or head_branch
        try:
            if target != head_branch:
                co = self._git(["checkout", target])
                if co.returncode != 0:
                    return {"error": f"切换目标分支失败: {co.stderr.strip()}", "target": target}
            merge_args = ["merge", "--no-ff", wt.branch]
            if message:
                merge_args += ["-m", message]
            res = self._git(merge_args)
            if res.returncode != 0:
                self._git(["merge", "--abort"])
                logger.warning(f"merge_back 失败已回滚: {name} -> {target}: {res.stderr.strip()}")
                return {"error": f"合并失败 (已 abort): {res.stderr.strip()}", "target": target}
            commit = self._git(["rev-parse", "HEAD"]).stdout.strip()
            logger.info(f"merge_back 完成: {name} ({wt.branch}) -> {target} @ {commit[:8]}")
            return {"merged": True, "branch": wt.branch, "target": target, "commit": commit}
        finally:
            if target != head_branch:
                back = self._git(["checkout", head_branch])
                if back.returncode != 0:
                    logger.error(f"恢复原分支失败: {head_branch}: {back.stderr.strip()}")

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
        wt = self._resolve(name)
        if not wt:
            return {"error": f"worktree 不存在: {name}"}
        # issue #580: 不可信来源门禁 — 外部来源 worktree 的 setup/exec 阻止到显式信任
        untrusted = self.is_untrusted(name)
        if untrusted:
            return {
                "error": f"worktree 来源不可信 (source={untrusted}), 拒绝执行; 需显式 trust_source 解锁",
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
