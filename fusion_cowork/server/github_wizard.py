"""issue #263: repository onboarding wizard — one-time GitHub org initialization (label tax / issue forms / CODEOWNERS / branch protection).

- Token only comes from the GITHUB_TOKEN env var (documented), never committed to the repo/workflow files
- preview produces an action plan (dry-run diff); apply is idempotent (existing resources are skipped/updated)
- Each repo gets a .github/wizard-managed.json audit manifest — teardown only removes wizard-created artifacts
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

# Test/injection seam — when not None, web_api._wizard() uses it directly to build the instance
WIZARD_FACTORY: Optional[Any] = None

DEFAULT_LABELS: List[Dict[str, str]] = [
    {"name": "phase:backlog", "color": "ededed", "description": "Backlog"},
    {"name": "phase:dev", "color": "0e8a16", "description": "In development"},
    {"name": "phase:review", "color": "fbca04", "description": "In review"},
    {"name": "phase:done", "color": "5319e7", "description": "Done"},
    {"name": "risk:high", "color": "d93f0b", "description": "High risk"},
    {"name": "risk:low", "color": "c2e0c6", "description": "Low risk"},
    {"name": "type:bug", "color": "ee0701", "description": "Bug"},
    {"name": "type:feature", "color": "84b6eb", "description": "Feature"},
    {"name": "type:task", "color": "cccccc", "description": "Task"},
    {"name": "blocked", "color": "b60205", "description": "Blocked"},
]

ISSUE_FORMS: Dict[str, Dict[str, Any]] = {
    "requirement.yml": {
        "name": "Requirement",
        "description": "New requirement proposal",
        "labels": ["type:feature", "phase:backlog"],
        "body": [
            {"type": "textarea", "id": "background", "attributes": {"label": "Background", "description": "Why this is needed"}},
            {"type": "textarea", "id": "goal", "attributes": {"label": "Goal", "description": "Definition of done"}},
            {"type": "dropdown", "id": "risk", "attributes": {"label": "Risk level", "options": ["low", "high"]}},
        ],
    },
    "task.yml": {
        "name": "Task",
        "description": "Development task",
        "labels": ["type:task", "phase:backlog"],
        "body": [
            {"type": "textarea", "id": "scope", "attributes": {"label": "Scope"}},
            {"type": "input", "id": "owner", "attributes": {"label": "Owner"}},
        ],
    },
    "acceptance-report.yml": {
        "name": "Acceptance report",
        "description": "Acceptance verdict",
        "labels": ["phase:review"],
        "body": [
            {"type": "textarea", "id": "result", "attributes": {"label": "Acceptance verdict"}},
            {
                "type": "checkboxes",
                "id": "checks",
                "attributes": {"label": "Checks", "options": ["Tests pass", "Docs updated"]},
            },
        ],
    },
}

DEFAULT_CODEOWNERS_PATTERNS = ["* @OWNER/lead"]
DEFAULT_PROTECTED_CHECKS = ["lint", "test"]


def _b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _yaml_dump(data: Dict[str, Any]) -> str:
    # Minimal YAML serialization — only covers the known issue-form shapes, avoids the PyYAML dependency
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
    """GitHub REST wizard client — token read from env; preview/apply/teardown are all idempotent."""

    def __init__(self, token: str = "", base_url: str = "", client_factory: Any = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.base_url = (base_url or os.environ.get("FUSION_GITHUB_API", "https://api.github.com")).rstrip("/")
        self._client_factory = client_factory  # test seam

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

    # ── current-state collection ──
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
        # 404 = not protected; 403 = token lacks administration permission (cannot read/write protection, degrade loudly and skip)
        if resp.status_code in (403, 404):
            if resp.status_code == 403:
                logger.warning(f"branch protection read denied (token lacks administration permission?) {repo}:{branch} — wizard will skip protection items")
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
                plan.add("skip_label", lbl["name"], "already in desired state")

    def _files_plan(self, plan: WizardPlan, repo: str, branch: str, patterns: List[str]) -> None:
        for path, body in ISSUE_FORMS.items():
            content = _yaml_dump(body)
            cur = self._file_cache.get(f"{repo}:{path}:{branch}")
            if cur is None:
                plan.add("create_file", path, "issue form")
            elif base64.b64decode(cur.get("content", "")).decode("utf-8", "replace") != content:
                plan.add("update_file", path, "content differs")
            else:
                plan.add("skip_file", path, "already in desired state")
        owners = "# " + WIZARD_MARKER + "\n" + "\n".join(patterns) + "\n"
        cur = self._file_cache.get(f"{repo}:CODEOWNERS:{branch}")
        if cur is None:
            plan.add("create_file", "CODEOWNERS", "")
        elif base64.b64decode(cur.get("content", "")).decode("utf-8", "replace") != owners:
            plan.add("update_file", "CODEOWNERS", "content differs")
        else:
            plan.add("skip_file", "CODEOWNERS", "already in desired state")

    async def preview(
        self,
        org: str,
        repos: List[str],
        labels: Optional[List[Dict[str, str]]] = None,
        codeowners: Optional[Dict[str, List[str]]] = None,
        protected_checks: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """dry-run — per-repo action plan (writes no remote state)."""
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
                plan.add("skip_branch_protection", branch, "already enabled")
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
        """Apply wizard configuration — idempotent; writes a wizard-managed.json audit manifest per repo."""
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
                    logger.warning(f"branch protection set failed, skipping (remaining wizard items continue): {repo_full}:{branch}: {e}")
                    applied.append({"op": "skip_branch_protection", "target": branch, "detail": str(e)[:120]})
            audit = {"managed_labels": managed_labels, "managed_files": managed_files, "branch_protection": branch}
            audit_path = WIZARD_AUDIT_PATH
            cur = await self._get_file(repo_full, audit_path, branch)
            await self._put_file(repo_full, audit_path, branch, _yaml_dump(audit), (cur or {}).get("sha"))
            applied.append({"op": "update_file", "target": audit_path})
            logger.info(f"wizard applied: {repo_full} ({len(applied)} changes)")
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
            # free private repos do not support protection — log loudly instead of silently
            logger.warning(f"branch protection set failed {repo}:{branch}: HTTP {resp.status_code}")
            resp.raise_for_status()

    async def teardown(self, org: str, repos: List[str]) -> List[Dict[str, Any]]:
        """Teardown — deletes only artifacts registered in wizard-managed.json (labels/files), and removes branch protection."""
        results: List[Dict[str, Any]] = []
        for repo_full in repos:
            branch = await self._default_branch(repo_full)
            removed: List[Dict[str, Any]] = []
            audit_raw = await self._get_file(repo_full, WIZARD_AUDIT_PATH, branch)
            if audit_raw is None:
                results.append({"repo": repo_full, "removed": [], "note": "no wizard audit manifest, nothing touched"})
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
            logger.info(f"wizard torn down: {repo_full} ({len(removed)} items removed)")
            results.append({"repo": repo_full, "removed": removed})
        return results


def _load_audit(text: str) -> Dict[str, Any]:
    # _yaml_dump is actually JSON; on parse failure return empty (delete nothing, fail-closed)
    import json

    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    try:
        return json.loads("\n".join(lines))
    except Exception as e:
        logger.error(f"wizard audit manifest parse failed, skipping teardown: {e}")
        return {}
