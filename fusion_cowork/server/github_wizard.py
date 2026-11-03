"""issue #263: 仓库 onboarding 向导 — GitHub org 一次性初始化 (label 税 / issue 表单 / CODEOWNERS / 分支保护)。

- Token 只走环境变量 GITHUB_TOKEN (文档化), 绝不入库/流程文件
- preview 产出动作计划 (dry-run diff); apply 幂等 (已有资源跳过/更新)
- 每仓库落 .github/wizard-managed.json 审计清单 — teardown 只删向导自建产物
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

WIZARD_AUDIT_PATH = ".github/wizard-managed.json"
WIZARD_MARKER = "managed-by: fusion-cowork-wizard"

# 测试/注入 seam — 非 None 时 web_api._wizard() 直接用它造实例
WIZARD_FACTORY: Optional[Any] = None

DEFAULT_LABELS: List[Dict[str, str]] = [
    {"name": "phase:backlog", "color": "ededed", "description": "待排期"},
    {"name": "phase:dev", "color": "0e8a16", "description": "开发中"},
    {"name": "phase:review", "color": "fbca04", "description": "评审中"},
    {"name": "phase:done", "color": "5319e7", "description": "已完成"},
    {"name": "risk:high", "color": "d93f0b", "description": "高风险"},
    {"name": "risk:low", "color": "c2e0c6", "description": "低风险"},
    {"name": "type:bug", "color": "ee0701", "description": "缺陷"},
    {"name": "type:feature", "color": "84b6eb", "description": "特性"},
    {"name": "type:task", "color": "cccccc", "description": "任务"},
    {"name": "blocked", "color": "b60205", "description": "被阻塞"},
]

ISSUE_FORMS: Dict[str, Dict[str, Any]] = {
    "requirement.yml": {
        "name": "需求",
        "description": "新需求提案",
        "labels": ["type:feature", "phase:backlog"],
        "body": [
            {"type": "textarea", "id": "background", "attributes": {"label": "背景", "description": "为什么做"}},
            {"type": "textarea", "id": "goal", "attributes": {"label": "目标", "description": "做到什么算完成"}},
            {"type": "dropdown", "id": "risk", "attributes": {"label": "风险等级", "options": ["low", "high"]}},
        ],
    },
    "task.yml": {
        "name": "任务",
        "description": "开发任务",
        "labels": ["type:task", "phase:backlog"],
        "body": [
            {"type": "textarea", "id": "scope", "attributes": {"label": "范围"}},
            {"type": "input", "id": "owner", "attributes": {"label": "负责人"}},
        ],
    },
    "acceptance-report.yml": {
        "name": "验收报告",
        "description": "验收结论",
        "labels": ["phase:review"],
        "body": [
            {"type": "textarea", "id": "result", "attributes": {"label": "验收结论"}},
            {
                "type": "checkboxes",
                "id": "checks",
                "attributes": {"label": "检查项", "options": ["测试通过", "文档更新"]},
            },
        ],
    },
}

DEFAULT_CODEOWNERS_PATTERNS = ["* @OWNER/lead"]
DEFAULT_PROTECTED_CHECKS = ["lint", "test"]


def _b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _yaml_dump(data: Dict[str, Any]) -> str:
    # 最小 YAML 序列化 — 只覆盖 issue-form 的已知形状, 避免 PyYAML 依赖
    import json

    return f"# {WIZARD_MARKER}\n" + json.dumps(data, ensure_ascii=False, indent=2)


@dataclass
class WizardPlan:
    org: str
    repo: str
    actions: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, op: str, target: str, detail: str = "") -> None:
        self.actions.append({"op": op, "target": target, "detail": detail})


class GithubWizard:
    """GitHub REST 向导客户端 — token 从 env 读取, 预览/应用/卸载均幂等。"""

    def __init__(self, token: str = "", base_url: str = "", client_factory: Any = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.base_url = (base_url or os.environ.get("FUSION_GITHUB_API", "https://api.github.com")).rstrip("/")
        self._client_factory = client_factory  # 测试 seam

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self, timeout: float = 15.0) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(timeout=timeout)
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers(), timeout=timeout)

    async def _req(self, method: str, path: str, json_body: Any = None) -> httpx.Response:
        async with self._client() as client:
            return await client.request(method, path, json=json_body)

    async def list_repos(self, org: str) -> List[Dict[str, Any]]:
        path = f"/orgs/{org}/repos?per_page=100" if await self._is_org(org) else f"/users/{org}/repos?per_page=100"
        resp = await self._req("GET", path)
        resp.raise_for_status()
        return [
            {
                "name": r["name"],
                "full_name": r["full_name"],
                "private": r["private"],
                "default_branch": r["default_branch"],
            }
            for r in resp.json()
        ]

    async def _is_org(self, org: str) -> bool:
        resp = await self._req("GET", f"/orgs/{org}")
        return resp.status_code == 200

    # ── 现状采集 ──
    async def _get_labels(self, repo: str) -> Dict[str, Dict[str, Any]]:
        resp = await self._req("GET", f"/repos/{repo}/labels?per_page=100")
        resp.raise_for_status()
        return {item["name"]: item for item in resp.json()}

    async def _get_file(self, repo: str, path: str, branch: str) -> Optional[Dict[str, Any]]:
        resp = await self._req("GET", f"/repos/{repo}/contents/{path}?ref={branch}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def _get_branch_protection(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        resp = await self._req("GET", f"/repos/{repo}/branches/{branch}/protection")
        # 404 = 未保护; 403 = token 无 administration 权限 (无法读写保护, 大声降级跳过)
        if resp.status_code in (403, 404):
            if resp.status_code == 403:
                logger.warning(f"分支保护读取被拒 (token 无 administration 权限?) {repo}:{branch} — 向导将跳过保护项")
            return None
        resp.raise_for_status()
        return resp.json()

    def _labels_plan(self, plan: WizardPlan, existing: Dict[str, Dict[str, Any]], labels: List[Dict[str, str]]) -> None:
        for lbl in labels:
            cur = existing.get(lbl["name"])
            if cur is None:
                plan.add("create_label", lbl["name"], lbl.get("color", ""))
            elif cur.get("color", "").lower() != lbl["color"].lower() or cur.get("description", "") != lbl.get(
                "description", ""
            ):
                plan.add("update_label", lbl["name"], f"{cur.get('color')}→{lbl['color']}")
            else:
                plan.add("skip_label", lbl["name"], "已是期望状态")

    def _files_plan(self, plan: WizardPlan, repo: str, branch: str, patterns: List[str]) -> None:
        for path, body in ISSUE_FORMS.items():
            content = _yaml_dump(body)
            cur = self._file_cache.get(f"{repo}:{path}:{branch}")
            if cur is None:
                plan.add("create_file", path, "issue form")
            elif base64.b64decode(cur.get("content", "")).decode("utf-8", "replace") != content:
                plan.add("update_file", path, "内容有差异")
            else:
                plan.add("skip_file", path, "已是期望状态")
        owners = "# " + WIZARD_MARKER + "\n" + "\n".join(patterns) + "\n"
        cur = self._file_cache.get(f"{repo}:CODEOWNERS:{branch}")
        if cur is None:
            plan.add("create_file", "CODEOWNERS", "")
        elif base64.b64decode(cur.get("content", "")).decode("utf-8", "replace") != owners:
            plan.add("update_file", "CODEOWNERS", "内容有差异")
        else:
            plan.add("skip_file", "CODEOWNERS", "已是期望状态")

    async def preview(
        self,
        org: str,
        repos: List[str],
        labels: Optional[List[Dict[str, str]]] = None,
        codeowners: Optional[Dict[str, List[str]]] = None,
        protected_checks: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """dry-run — 每仓库动作计划 (不写任何远端状态)。"""
        labels = labels if labels is not None else DEFAULT_LABELS
        protected_checks = protected_checks or DEFAULT_PROTECTED_CHECKS
        plans: List[Dict[str, Any]] = []
        for repo_full in repos:
            branch = await self._default_branch(repo_full)
            plan = WizardPlan(org=org, repo=repo_full)
            self._labels_plan(plan, await self._get_labels(repo_full), labels)
            self._file_cache = {}
            for path in list(ISSUE_FORMS) + ["CODEOWNERS"]:
                fpath = path if path == "CODEOWNERS" else f".github/ISSUE_TEMPLATE/{path}"
                self._file_cache[f"{repo_full}:{path}:{branch}"] = await self._get_file(repo_full, fpath, branch)
            pats = (codeowners or {}).get(repo_full, DEFAULT_CODEOWNERS_PATTERNS)
            self._files_plan(plan, repo_full, branch, pats)
            protection = await self._get_branch_protection(repo_full, branch)
            if protection is None:
                plan.add("set_branch_protection", branch, f"required checks: {protected_checks}")
            else:
                plan.add("skip_branch_protection", branch, "已启用")
            plans.append({"repo": repo_full, "branch": branch, "actions": plan.actions})
        return plans

    async def _default_branch(self, repo: str) -> str:
        resp = await self._req("GET", f"/repos/{repo}")
        resp.raise_for_status()
        return str(resp.json().get("default_branch") or "main")

    async def _put_file(self, repo: str, path: str, branch: str, content: str, sha: Optional[str]) -> None:
        body: Dict[str, Any] = {"message": f"chore(wizard): sync {path}", "content": _b64(content), "branch": branch}
        if sha:
            body["sha"] = sha
        resp = await self._req("PUT", f"/repos/{repo}/contents/{path}", body)
        resp.raise_for_status()

    async def apply(
        self,
        org: str,
        repos: List[str],
        labels: Optional[List[Dict[str, str]]] = None,
        codeowners: Optional[Dict[str, List[str]]] = None,
        protected_checks: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """应用向导配置 — 幂等; 每仓库写 wizard-managed.json 审计清单。"""
        labels = labels if labels is not None else DEFAULT_LABELS
        protected_checks = protected_checks or DEFAULT_PROTECTED_CHECKS
        results: List[Dict[str, Any]] = []
        for repo_full in repos:
            branch = await self._default_branch(repo_full)
            applied: List[Dict[str, Any]] = []
            existing = await self._get_labels(repo_full)
            managed_labels: List[str] = []
            for lbl in labels:
                cur = existing.get(lbl["name"])
                if cur is None:
                    resp = await self._req("POST", f"/repos/{repo_full}/labels", lbl)
                    resp.raise_for_status()
                    applied.append({"op": "create_label", "target": lbl["name"]})
                elif cur.get("color", "").lower() != lbl["color"].lower() or cur.get("description", "") != lbl.get(
                    "description", ""
                ):
                    resp = await self._req("PATCH", f"/repos/{repo_full}/labels/{lbl['name']}", lbl)
                    resp.raise_for_status()
                    applied.append({"op": "update_label", "target": lbl["name"]})
                managed_labels.append(lbl["name"])
            managed_files: List[str] = []
            pats = (codeowners or {}).get(repo_full, DEFAULT_CODEOWNERS_PATTERNS)
            for path, content, fpath in [
                (p, _yaml_dump(b), f".github/ISSUE_TEMPLATE/{p}") for p, b in ISSUE_FORMS.items()
            ] + [("CODEOWNERS", "# " + WIZARD_MARKER + "\n" + "\n".join(pats) + "\n", "CODEOWNERS")]:
                cur = await self._get_file(repo_full, fpath, branch)
                if cur is None:
                    await self._put_file(repo_full, fpath, branch, content, None)
                    applied.append({"op": "create_file", "target": fpath})
                elif base64.b64decode(cur.get("content", "")).decode("utf-8", "replace") != content:
                    await self._put_file(repo_full, fpath, branch, content, cur.get("sha"))
                    applied.append({"op": "update_file", "target": fpath})
                managed_files.append(fpath)
            protection = await self._get_branch_protection(repo_full, branch)
            if protection is None:
                try:
                    await self._set_branch_protection(repo_full, branch, protected_checks)
                    applied.append({"op": "set_branch_protection", "target": branch})
                except Exception as e:
                    logger.warning(f"分支保护设置失败, 跳过 (其余向导项继续): {repo_full}:{branch}: {e}")
                    applied.append({"op": "skip_branch_protection", "target": branch, "detail": str(e)[:120]})
            audit = {"managed_labels": managed_labels, "managed_files": managed_files, "branch_protection": branch}
            audit_path = WIZARD_AUDIT_PATH
            cur = await self._get_file(repo_full, audit_path, branch)
            await self._put_file(repo_full, audit_path, branch, _yaml_dump(audit), (cur or {}).get("sha"))
            applied.append({"op": "update_file", "target": audit_path})
            logger.info(f"向导已应用: {repo_full} ({len(applied)} 项变更)")
            results.append({"repo": repo_full, "branch": branch, "applied": applied})
        return results

    async def _set_branch_protection(self, repo: str, branch: str, checks: List[str]) -> None:
        body = {
            "required_status_checks": {"strict": True, "contexts": checks},
            "enforce_admins": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }
        resp = await self._req("PUT", f"/repos/{repo}/branches/{branch}/protection", body)
        if resp.status_code >= 400:
            # 免费私有仓不支持保护 — 大声记录而非静默
            logger.warning(f"分支保护设置失败 {repo}:{branch}: HTTP {resp.status_code}")
            resp.raise_for_status()

    async def teardown(self, org: str, repos: List[str]) -> List[Dict[str, Any]]:
        """卸载 — 只删 wizard-managed.json 登记的产物 (标签/文件), 分支保护关闭。"""
        results: List[Dict[str, Any]] = []
        for repo_full in repos:
            branch = await self._default_branch(repo_full)
            removed: List[Dict[str, Any]] = []
            audit_raw = await self._get_file(repo_full, WIZARD_AUDIT_PATH, branch)
            if audit_raw is None:
                results.append({"repo": repo_full, "removed": [], "note": "无向导审计清单, 未触碰"})
                continue
            audit = _load_audit(base64.b64decode(audit_raw.get("content", "")).decode("utf-8", "replace"))
            existing = await self._get_labels(repo_full)
            for name in audit.get("managed_labels", []):
                if name in existing:
                    resp = await self._req("DELETE", f"/repos/{repo_full}/labels/{name}")
                    if resp.status_code < 300:
                        removed.append({"op": "delete_label", "target": name})
            for fpath in audit.get("managed_files", []):
                cur = await self._get_file(repo_full, fpath, branch)
                if cur is not None:
                    resp = await self._req(
                        "DELETE",
                        f"/repos/{repo_full}/contents/{fpath}",
                        {"message": "chore(wizard): teardown", "sha": cur.get("sha"), "branch": branch},
                    )
                    if resp.status_code < 300:
                        removed.append({"op": "delete_file", "target": fpath})
            resp = await self._req("DELETE", f"/repos/{repo_full}/branches/{branch}/protection")
            if resp.status_code < 300:
                removed.append({"op": "unset_branch_protection", "target": branch})
            resp = await self._req(
                "DELETE",
                f"/repos/{repo_full}/contents/{WIZARD_AUDIT_PATH}",
                {"message": "chore(wizard): remove audit", "sha": audit_raw.get("sha"), "branch": branch},
            )
            if resp.status_code < 300:
                removed.append({"op": "delete_file", "target": WIZARD_AUDIT_PATH})
            logger.info(f"向导已卸载: {repo_full} ({len(removed)} 项移除)")
            results.append({"repo": repo_full, "removed": removed})
        return results


def _load_audit(text: str) -> Dict[str, Any]:
    # _yaml_dump 实为 JSON; 兼容解析失败 → 空 (不删任何东西, fail-closed)
    import json

    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    try:
        return json.loads("\n".join(lines))
    except Exception as e:
        logger.error(f"向导审计清单解析失败, 跳过卸载: {e}")
        return {}
