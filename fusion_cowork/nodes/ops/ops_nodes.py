"""issue #262: official generic node library group 1 — agent_exec / github_api / command_evidence / http_webhook / approval_gate.

Design constraints (issue #262):
- All configuration is per-flow-instance data, zero business constants in node code
- Tokens only via environment variables (repo allowlist / ${ENV} interpolation / HMAC secret env), never in flow definitions
- Allowlist mismatch is rejected loudly (fail-loud), never silently allowed
- approval_gate is approval-native: registered in HIGH_RISK_NODES into the platform HITL gate (#260 blocked steps visible)
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

# Built-in secret shapes (capture redaction + output fallback), covering common token families
_BUILTIN_SECRET_PATTERNS = (
    r"ghp_[A-Za-z0-9]{20,}",
    r"gh[ours]_[A-Za-z0-9]{20,}",
    r"sk-[A-Za-z0-9_-]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"Bearer\s+[A-Za-z0-9._-]{12,}",
    r"(?i)(password|passwd|secret|token)\s*[=:]\s*\S+",
)


def _scrub(text: str, extra_patterns: List[str]) -> str:
    patterns = list(_BUILTIN_SECRET_PATTERNS) + [p for p in extra_patterns if p]
    for pat in patterns:
        try:
            text = re.sub(pat, "[REDACTED]", text)
        except re.error as e:
            logger.warning(f"Invalid redaction regex, skipping: {pat} ({e})")
    return text


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[truncated]...\n{text[-limit:]}"


@register_node
class AgentExecNode(BaseNode):
    """External headless agent/CLI process execution — captures exit code/output, reports crashes structurally.

    Process lifecycle is not re-implemented here (issue #262 contract): crash = node FAILED,
    restart policy is owned by the caller via node re-run / new run, state lives in external storage.
    """

    name = "agent_exec"
    # i18n: user-facing
    display_name = "Agent 执行"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "以 argv 启动外部无头 agent/CLI 进程, 捕获退出码与输出"
    icon = "🤖"
    # i18n: user-facing
    default_label = "Agent 执行"

    # i18n: user-facing
    inputs = [{"key": "command", "label": "命令 argv", "type": "list[string]"}]
    outputs = [
        # i18n: user-facing
        {"key": "exit_code", "label": "退出码", "type": "number"},
        # i18n: user-facing
        {"key": "output", "label": "输出 (尾段)", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "command": {"type": "array", "items": {"type": "string"}, "description": "argv 列表 (不经 shell)"},
                # i18n: user-facing
                "cwd": {"type": "string", "description": "工作目录 (须已存在)"},
                # i18n: user-facing
                "timeout": {"type": "number", "description": "秒", "default": 120},
                # i18n: user-facing
                "env_keys": {"type": "array", "items": {"type": "string"}, "description": "透传的环境变量名白名单"},
                # i18n: user-facing
                "max_output": {"type": "number", "description": "输出保留字符数", "default": 20000},
            },
            "required": ["command"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        command = inputs.get("command") or params.get("command") or []
        cwd = str(params.get("cwd") or "")
        timeout = float(params.get("timeout") or 120)
        env_keys = list(params.get("env_keys") or [])
        max_output = int(params.get("max_output") or 20000)

        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            return NodeResult(status=NodeStatus.FAILED, error="command must be a non-empty string array (argv, no shell)")
        if cwd:
            if not Path(cwd).is_dir():
                return NodeResult(status=NodeStatus.FAILED, error=f"Working directory does not exist: {cwd}")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "en_US.UTF-8",
        }
        for k in env_keys:
            if k in os.environ:
                env[k] = os.environ[k]

        logger.info(f"agent_exec start: argv0={command[0]} cwd={cwd or '.'} timeout={timeout:g}s")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd or None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"Process launch failed: {e}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"agent_exec killed on timeout: argv0={command[0]} timeout={timeout:g}s")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"agent process timed out ({timeout:g}s), terminated", data={"exit_code": None}
            )
        output = _tail(stdout.decode("utf-8", "replace"), max_output)
        code = proc.returncode
        if code == 0:
            logger.info(f"agent_exec ok: argv0={command[0]} exit=0")
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"exit_code": 0, "output": output}, summary="agent process succeeded"
            )
        if code is not None and code < 0:
            logger.error(f"agent_exec crashed: argv0={command[0]} signal={-code}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent process crashed (signal={-code}); restart policy: re-run this node to restart, state is owned by external storage",
                data={"exit_code": code, "output": output},
            )
        logger.warning(f"agent_exec non-zero exit: argv0={command[0]} exit={code}")
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"agent process exit code {code}",
            data={"exit_code": code, "output": output},
            summary=f"Exit code {code}",
        )


@register_node
class GitHubNode(BaseNode):
    """GitHub API node — issue/PR/label operations, org/repo allowlist + environment variable token."""

    name = "github_api"
    display_name = "GitHub"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "GitHub issue/PR/label 操作; 仓库白名单 + GITHUB_TOKEN 环境变量鉴权"
    icon = "🐙"
    default_label = "GitHub"

    inputs = [
        # i18n: user-facing
        {"key": "repo", "label": "仓库 (org/repo)", "type": "string"},
        # i18n: user-facing
        {"key": "issue_number", "label": "Issue/PR 编号", "type": "number"},
    ]
    # i18n: user-facing
    outputs = [{"key": "result", "label": "API 结果", "type": "object"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "issue_create",
                        "issue_comment",
                        "issue_set_labels",
                        "issue_list",
                        "pr_get",
                        "list_labels",
                        "linked_pr",
                    ],
                    # i18n: user-facing
                    "description": "操作",
                },
                # i18n: user-facing
                "repo": {"type": "string", "description": "org/repo"},
                # i18n: user-facing
                "allowed_repos": {"type": "array", "items": {"type": "string"}, "description": "仓库白名单"},
                # i18n: user-facing
                "issue_number": {"type": "number", "description": "issue/PR 编号"},
                "require": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "linked_pr 专用: 无关联 PR 时节点失败 (显式阻塞, 重跑即复查)",
                },
                "require_closing_link": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        # i18n: user-facing
                        "linked_pr 专用: PR 仅在正文提及 issue (无 Closes/Fixes/Resolves 关闭关键词) 时节点失败 — "
                        # i18n: user-facing
                        "拦截 hygiene/aside PR 冒充交付 (issue #362)"
                    ),
                },
                "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number", "default": 30},
                # issue #387: synthetic E2E — dry_run returns a deterministic canned result, zero network and zero tokens
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "合成模式: 不调 GitHub API, 返回确定性伪结果 (合成 E2E 管线用)",
                },
            },
            "required": ["op", "repo", "allowed_repos"],
        }

    def _client(self, base_url: str, token: str, timeout: float) -> httpx.AsyncClient:
        # Test seam: monkeypatch this method to inject a MockTransport
        return httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=timeout,
        )

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        op = str(inputs.get("op") or params.get("op") or "")
        repo = str(inputs.get("repo") or params.get("repo") or "")
        allowed = list(params.get("allowed_repos") or [])
        issue_number = int(inputs.get("issue_number") or params.get("issue_number") or 0)
        title = str(params.get("title") or "")
        body = str(params.get("body") or "")
        # issue #427: empty-label filtering — templates interpolate extension labels per verdict, empty string on non-extend runs
        labels = [str(x) for x in (params.get("labels") or []) if str(x).strip()]
        timeout = float(params.get("timeout") or 30)

        if op not in (
            "issue_create",
            "issue_comment",
            "issue_set_labels",
            "issue_list",
            "pr_get",
            "list_labels",
            "linked_pr",
        ):
            return NodeResult(status=NodeStatus.FAILED, error=f"Unknown operation: {op}")
        if repo not in allowed:
            logger.warning(f"github_api rejected: {repo} not in allowlist {allowed}")
            return NodeResult(status=NodeStatus.FAILED, error=f"Repository {repo} not in allowed_repos allowlist")
        # issue #387: synthetic E2E — validation still goes through allowlist/op checks, but zero network, returns a deterministic canned result
        dry_run = params.get("dry_run", False)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")
        if dry_run:
            logger.info(f"github_api dry_run synthetic mode: {op} {repo}#{issue_number or '-'}")
            canned: Dict[str, Any]
            if op == "issue_create":
                canned = {
                    "number": 1,
                    "title": title,
                    "html_url": f"https://github.invalid/{repo}/issues/1",
                    "state": "open",
                }
            elif op == "linked_pr":
                canned = {
                    "found": True,
                    "pr_url": f"https://github.invalid/{repo}/pull/1",
                    "pr_number": 1,
                    "ci_status": "success",
                    "pr_link_strength": "closing",
                }
            elif op == "pr_get":
                canned = {"number": issue_number, "state": "open"}
            else:
                canned = {}
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"result": canned, "dry_run": True},
                summary=f"{op} {repo} (dry_run synthetic)",
            )
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            return NodeResult(status=NodeStatus.FAILED, error="GITHUB_TOKEN not configured (tokens only via env vars, never in flow definitions)")
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")

        method, path, payload = "GET", "", None
        if op == "issue_create":
            method, path, payload = "POST", f"/repos/{repo}/issues", {"title": title, "body": body, "labels": labels}
            if not title:
                return NodeResult(status=NodeStatus.FAILED, error="issue_create missing title")
        elif op == "issue_comment":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="issue_comment missing issue_number")
            method, path, payload = "POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body}
        elif op == "issue_set_labels":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="issue_set_labels missing issue_number")
            method, path, payload = "PUT", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": labels}
        elif op == "pr_get":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="pr_get missing issue_number")
            method, path = "GET", f"/repos/{repo}/pulls/{issue_number}"
        elif op == "linked_pr":
            # issue #306: linked-PR precheck — pure REST reads (zero LLM), evidence gate for the G4 acceptance gate.
            # timeline contains cross-referenced events; source.issue with pull_request is a linked PR;
            # when a PR is found, best-effort fetch of CI status (failure does not block, left empty).
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="linked_pr missing issue_number")
            method, path = "GET", f"/repos/{repo}/issues/{issue_number}/timeline"
        elif op == "issue_list":
            # issue #283: list issues by label (governance batch fan-out intake)
            labels = ",".join(labels) if labels else ""
            query = f"?labels={labels}" if labels else ""
            method, path = "GET", f"/repos/{repo}/issues{query}"

        logger.info(f"github_api {op} {repo}#{issue_number or '-'}")
        try:
            async with self._client(base_url, token, timeout) as client:
                # issue #352: issue_create idempotency — search for a same-title issue first, reuse on hit instead of recreating
                # (pipeline re-runs no longer duplicate task issues; exact title equality counts as a hit)
                # issue #368: query all states (open+closed) — a same-title issue closed after delivery is also a duplicate,
                # querying only open would create a fake task again on re-run after the PR merge closes it
                if op == "issue_create":
                    # issue #409: GitHub search q cap of 256 chars — truncate long titles (best-effort dedup,
                    # not identity judgment; issue creation uses the full title, unaffected); truncation must not eat an escaped backslash
                    q_fixed = len(f'repo:{repo} is:issue "') + 1
                    q_title = title.replace(chr(34), chr(92) + chr(34))
                    if q_fixed + len(q_title) > 256:
                        q_title = q_title[: max(0, 256 - q_fixed)].rstrip("\\").rstrip()
                        logger.info("github_api issue_create dedupe search q truncated to 256 chars (long title)")
                    q = f'repo:{repo} is:issue "{q_title}"'
                    sresp = await client.request("GET", "/search/issues", params={"q": q})
                    sresp.raise_for_status()
                    for item in (sresp.json() or {}).get("items") or []:
                        if isinstance(item, dict) and str(item.get("title") or "") == title:
                            logger.info(
                                f"github_api issue_create idempotent reuse: {repo}#{item.get('number')} "
                                f"(same-title issue already exists, state={item.get('state')})"
                            )
                            return NodeResult(
                                status=NodeStatus.SUCCESS,
                                data={"result": dict(item), "deduplicated": True, "state": item.get("state") or ""},
                                summary=f"Issue already exists ({item.get('state')}), reusing #{item.get('number')}: {title[:60]}",
                            )

                # issue #382: write operations (POST/PUT) go through outbox receipts — replays of the same run/node/op
                # (crash recovery/retry) no longer write twice; read operations skip receipts
                async def _do_write():
                    r = await client.request(method, path, json=payload)
                    r.raise_for_status()
                    return r.json()

                from fusion_cowork.engine.outbox import get_outbox_bridge, outbox_ctx

                obctx = outbox_ctx.get()
                _ob = get_outbox_bridge() if obctx else None
                if _ob is not None and method in ("POST", "PUT"):
                    cmd_id = f"{obctx.get('run_id', '')}:{obctx.get('node_id', '')}:{op}"
                    if op == "issue_create" and title:
                        cmd_id = f"{cmd_id}:{title}"
                    data, _replayed = await _ob.execute_idempotent_async(cmd_id, f"github:{repo}", _do_write)
                    if _replayed:
                        logger.info(f"github_api {op} {repo} outbox receipt replay, skipping duplicate write")
                else:
                    resp = await client.request(method, path, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"github_api HTTP {e.response.status_code}: {op} {repo}")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"GitHub API HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:
            logger.warning(f"github_api failed: {op} {repo}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"GitHub API call failed: {e}")
        result = data if isinstance(data, (dict, list)) else {}
        if isinstance(result, dict):
            result.pop("diff_url", None)
        if op == "linked_pr":
            # issue #306: scan timeline events for a cross-referenced linked PR; require semantics —
            # no PR means failure (explicit 'PR first, then acceptance'), re-running the node rechecks.
            found_url, found_num = "", 0
            for evt in result if isinstance(result, list) else []:
                if not isinstance(evt, dict) or evt.get("event") != "cross-referenced":
                    continue
                src_issue = ((evt.get("source") or {}).get("issue")) or {}
                if src_issue.get("pull_request") or "/pull/" in str(src_issue.get("html_url") or ""):
                    found_url = str(src_issue.get("html_url") or "")
                    found_num = int(src_issue.get("number") or 0)
                    break
            ci_status = ""
            link_strength = ""
            if found_num:
                ci_status = await self._pr_ci_status(base_url, token, timeout, repo, found_num)
                # issue #362: link strength — only closing (PR body contains Closes/Fixes/Resolves #N) counts
                # as a delivery link; mention (body mention only) logs WARN, optional require_closing_link blocks
                link_strength = await self._pr_link_strength(base_url, token, timeout, repo, found_num, issue_number)
                if link_strength == "mention":
                    logger.warning(
                        f"github_api linked_pr: {repo}#{issue_number} PR#{found_num} only mentions the issue "
                        "(no closing keywords), pr_link_strength=mention"
                    )
            if bool(params.get("require")) and not found_url:
                # issue #368: issue already closed by the dispatched session (duplicate/invalid disposition, no PR delivery)
                # does not count as an acceptance block — no-op delivery passes; state query failure (empty string) follows the original blocking path
                state = await self._issue_state(base_url, token, timeout, repo, issue_number)
                if state == "closed":
                    logger.info(f"github_api linked_pr: {repo}#{issue_number} no PR but issue already closed (no-op delivery, passing)")
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={
                            "result": {
                                "found": False,
                                "pr_url": "",
                                "pr_number": 0,
                                "ci_status": "",
                                "pr_link_strength": "",
                                "issue_closed": True,
                            }
                        },
                        summary=f"Issue #{issue_number} closed with no linked PR (no-op delivery)",
                    )
                logger.warning(f"github_api linked_pr: {repo}#{issue_number} no linked PR (require=true, blocking)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="Open a PR before acceptance (issue has no linked PR, G4 cannot self-certify)",
                    data={"found": False, "pr_url": "", "pr_number": 0, "ci_status": ""},
                    summary="No linked PR, blocking acceptance",
                )
            if bool(params.get("require_closing_link")) and found_url and link_strength == "mention":
                logger.warning(
                    f"github_api linked_pr: {repo}#{issue_number} PR#{found_num} no closing link (require_closing_link=true, blocking)"
                )
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"PR#{found_num} only mentions the issue without a closing-keyword link (needs Closes/Fixes/Resolves #{issue_number} "
                        "in the PR body), does not count as a delivery PR (issue #362)"
                    ),
                    data={
                        "found": True,
                        "pr_url": found_url,
                        "pr_number": found_num,
                        "ci_status": ci_status,
                        "pr_link_strength": link_strength,
                    },
                    summary="PR has no closing link, blocking acceptance",
                )
            logger.info(
                f"github_api linked_pr: {repo}#{issue_number} found={found_num or False} ci={ci_status or '-'} link={link_strength or '-'}"
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "result": {
                        "found": bool(found_url),
                        "pr_url": found_url,
                        "pr_number": found_num,
                        "ci_status": ci_status,
                        "pr_link_strength": link_strength,
                    }
                },
                summary=f"linked_pr {'✓ ' + found_url if found_url else 'no linked PR'}",
            )
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": result}, summary=f"{op} {repo} ok")

    async def _pr_ci_status(self, base_url: str, token: str, timeout: float, repo: str, pr_number: int) -> str:
        # issue #306: best-effort fetch of PR CI status (combined status); any failure returns an empty string without blocking
        try:
            async with self._client(base_url, token, timeout) as client:
                pr = (await client.request("GET", f"/repos/{repo}/pulls/{pr_number}")).json()
                sha = str(((pr or {}).get("head") or {}).get("sha") or "")
                if not sha:
                    return ""
                st = (await client.request("GET", f"/repos/{repo}/commits/{sha}/status")).json()
                return str((st or {}).get("state") or "")
        except Exception as e:
            logger.info(f"github_api linked_pr: CI status fetch failed (ignored): {e}")
            return ""

    async def _pr_link_strength(
        self, base_url: str, token: str, timeout: float, repo: str, pr_number: int, issue_number: int
    ) -> str:
        # issue #362: closing = PR body links this issue with closing keywords; mention = mention only; failure degrades to mention
        try:
            async with self._client(base_url, token, timeout) as client:
                pr = (await client.request("GET", f"/repos/{repo}/pulls/{pr_number}")).json()
                body = str((pr or {}).get("body") or "")
            pattern = rf"\b(?:close[sd]?|fix(?:e[sd]?)?|resolve[sd]?)\s*:?\s*#{issue_number}\b"
            return "closing" if re.search(pattern, body, re.IGNORECASE) else "mention"
        except Exception as e:
            logger.info(f"github_api linked_pr: PR body fetch failed (degrading to mention): {e}")
            return "mention"

    async def _issue_state(self, base_url: str, token: str, timeout: float, repo: str, issue_number: int) -> str:
        # issue #368: best-effort fetch of issue state; failure returns an empty string (caller follows the original blocking path, no pass-through)
        try:
            async with self._client(base_url, token, timeout) as client:
                issue = (await client.request("GET", f"/repos/{repo}/issues/{issue_number}")).json()
            return str((issue or {}).get("state") or "")
        except Exception as e:
            logger.info(f"github_api linked_pr: issue state fetch failed (treated as not closed): {e}")
            return ""


@register_node
class CommandEvidenceNode(BaseNode):
    """Allowlisted command execution + evidence archiving — port reconciliation / liveness probing / secret redaction."""

    name = "command_evidence"
    # i18n: user-facing
    display_name = "命令取证"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "白名单命令执行, stdout/stderr 脱敏后归档证据目录, 附端口对账与存活探测"
    icon = "🧾"
    # i18n: user-facing
    default_label = "命令取证"

    # i18n: user-facing
    inputs = [{"key": "command", "label": "命令 argv", "type": "list[string]"}]
    outputs = [
        # i18n: user-facing
        {"key": "exit_code", "label": "退出码", "type": "number"},
        # i18n: user-facing
        {"key": "evidence_path", "label": "证据文件", "type": "string"},
        # i18n: user-facing
        {"key": "ports", "label": "端口对账", "type": "list[object]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "command": {"type": "array", "items": {"type": "string"}, "description": "argv (不经 shell)"},
                "allowlist": {
                    "type": "array",
                    "items": {"type": "string"},
                    # i18n: user-facing
                    "description": "fnmatch 模式, 匹配 'argv 全拼' / argv0 basename / basename 去版本后缀 (python3.12 归一为 python)",
                },
                # i18n: user-facing
                "evidence_dir": {"type": "string", "description": "证据归档目录"},
                # i18n: user-facing
                "ports": {"type": "array", "items": {"type": "number"}, "description": "对账/探测端口"},
                # i18n: user-facing
                "port_registry": {"type": "string", "description": "端口登记 JSON ({port: service}) 路径"},
                # i18n: user-facing
                "redact": {"type": "array", "items": {"type": "string"}, "description": "额外脱敏正则"},
                "timeout": {"type": "number", "default": 120},
                "max_output": {"type": "number", "default": 20000},
            },
            "required": ["command", "allowlist", "evidence_dir"],
        }

    async def _probe_port(self, port: int) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            return False

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        command = inputs.get("command") or params.get("command") or []
        allowlist = [str(p) for p in (params.get("allowlist") or [])]
        evidence_dir = str(params.get("evidence_dir") or "")
        ports = [int(p) for p in (params.get("ports") or [])]
        port_registry = str(params.get("port_registry") or "")
        extra_redact = [str(p) for p in (params.get("redact") or [])]
        timeout = float(params.get("timeout") or 120)
        max_output = int(params.get("max_output") or 20000)

        joined = " ".join(command) if isinstance(command, list) else str(command)
        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            return NodeResult(status=NodeStatus.FAILED, error="command must be a non-empty string array (argv, no shell)")
        if not allowlist:
            return NodeResult(status=NodeStatus.FAILED, error="allowlist is empty — refusing to execute (fail-loud, no pass-through)")
        # Allowlist matches three shapes: full argv (with absolute path) + argv0 basename + basename without version suffix
        # (issue #334: venv/pyenv interpreters are often python3.12/python3 — same family as `python *`, normalized match)
        argv0_base = Path(command[0]).name
        argv0_family = argv0_base.rstrip(".0123456789")
        rest_joined = " ".join(str(c) for c in command[1:])
        candidates = (joined, " ".join([argv0_base, rest_joined]), " ".join([argv0_family, rest_joined]))
        if not any(fnmatch.fnmatch(cand, pat) for cand in candidates for pat in allowlist):
            logger.warning(f"command_evidence rejected non-allowlisted command: {joined}")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"Command not in allowlist, rejected: {joined} (allowlist={allowlist})"
            )
        if not evidence_dir:
            return NodeResult(status=NodeStatus.FAILED, error="evidence_dir is required")

        logger.info(f"command_evidence start: {joined}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return NodeResult(status=NodeStatus.FAILED, error=f"Command timed out ({timeout:g}s), terminated")
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"Command launch failed: {e}")

        raw = stdout.decode("utf-8", "replace")
        scrubbed = _scrub(raw, extra_redact)
        out_path = Path(evidence_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(command[0]).name}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = _scrub("$ " + " ".join(command), extra_redact)
        out_path.write_text(
            f"{header}\nexit={proc.returncode}\nat={time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n{scrubbed}\n",
            encoding="utf-8",
        )
        logger.info(f"command_evidence archived: {out_path} exit={proc.returncode}")

        port_report: List[Dict[str, Any]] = []
        registry: Dict[str, Any] = {}
        if port_registry and Path(port_registry).is_file():
            try:
                registry = json.loads(Path(port_registry).read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Port registry file unreadable (ignored): {e}")
        for port in ports:
            live = await self._probe_port(port)
            expected = registry.get(str(port), registry.get(port))
            port_report.append({"port": port, "expected": expected, "live": live})
            if expected is not None and bool(live) != bool(expected):
                logger.warning(f"command_evidence port reconciliation mismatch: {port} registered={expected} live={live}")

        output_tail = _tail(scrubbed, max_output)
        if proc.returncode == 0:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"exit_code": 0, "evidence_path": str(out_path), "ports": port_report, "output": output_tail},
                summary=f"exit=0, evidence archived ({len(port_report)} port reconciliations)",
            )
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"Command exit code {proc.returncode}",
            data={
                "exit_code": proc.returncode,
                "evidence_path": str(out_path),
                "ports": port_report,
                "output": output_tail,
            },
        )


@register_node
class HttpWebhookNode(BaseNode):
    """HTTP/Webhook call — retry policy + outbound HMAC signing; ${ENV} interpolation keeps tokens out of flows."""

    name = "http_webhook"
    display_name = "HTTP Webhook"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "调用外部 HTTP API (重试 + HMAC 出站签名); 入站回调走 /api/hooks/webhook/{run_id} 续跑审批闸门"
    icon = "🔗"
    default_label = "HTTP Webhook"

    # i18n: user-facing
    inputs = [{"key": "body", "label": "请求体", "type": "object"}]
    outputs = [
        # i18n: user-facing
        {"key": "status_code", "label": "HTTP 状态码", "type": "number"},
        # i18n: user-facing
        {"key": "response", "label": "响应 (截断)", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "url": {"type": "string", "description": "http/https 目标"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT"], "default": "POST"},
                # i18n: user-facing
                "headers": {"type": "object", "description": "值支持 ${ENV_NAME} 运行时插值"},
                # i18n: user-facing
                "retry": {"type": "number", "default": 2, "description": "网络错误/5xx 重试次数"},
                "retry_backoff": {"type": "number", "default": 1.0},
                "timeout": {"type": "number", "default": 30},
                "sign_secret_env": {
                    "type": "string",
                    "default": "FUSION_COWORK_WEBHOOK_SECRET",
                    # i18n: user-facing
                    "description": "出站 HMAC secret 的环境变量名",
                },
                "body": {"type": "object"},
            },
            "required": ["url"],
        }

    def _resolve_header(self, value: str) -> str:
        m = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", str(value))
        if not m:
            return str(value)
        env_name = m.group(1)
        resolved = os.environ.get(env_name, "")
        if not resolved:
            raise RuntimeError(f"Environment variable referenced by header {env_name} is empty — tokens only via env vars")
        return resolved

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = str(inputs.get("url") or params.get("url") or "")
        method = str(params.get("method") or "POST").upper()
        raw_headers = dict(params.get("headers") or {})
        retry = int(params.get("retry") or 2)
        backoff = float(params.get("retry_backoff") or 1.0)
        timeout = float(params.get("timeout") or 30)
        sign_env = str(params.get("sign_secret_env") or "FUSION_COWORK_WEBHOOK_SECRET")
        body = inputs.get("body", params.get("body"))
        body = body if body is not None else {}

        if not url.startswith(("http://", "https://")):
            return NodeResult(
                status=NodeStatus.FAILED, error=f"Invalid URL scheme: {url.split(':')[0] if ':' in url else url}"
            )
        try:
            headers = {str(k): self._resolve_header(v) for k, v in raw_headers.items()}
        except RuntimeError as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e))

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        secret = os.environ.get(sign_env, "").strip()
        send_headers = dict(headers)
        if secret:
            sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            send_headers["X-Fusion-Signature"] = f"sha256={sig}"

        logger.info(f"http_webhook {method} {url} retry={retry} signed={bool(secret)}")
        attempts = 0
        last_err = ""
        while attempts <= retry:
            attempts += 1
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.request(method, url, content=payload, headers=send_headers)
                if resp.status_code >= 500 and attempts <= retry:
                    logger.warning(f"http_webhook 5xx ({resp.status_code}), backing off {backoff:g}s before retry {attempts}")
                    await asyncio.sleep(backoff)
                    continue
                truncated: Any
                try:
                    truncated = resp.json()
                    if isinstance(truncated, dict) and len(json.dumps(truncated)) > 4000:
                        truncated = {"_truncated": True}
                except Exception:
                    truncated = {"text": _tail(resp.text, 2000)}
                if resp.status_code >= 400:
                    logger.warning(f"http_webhook HTTP {resp.status_code}: {url}")
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"HTTP {resp.status_code}",
                        data={"status_code": resp.status_code, "response": truncated, "attempts": attempts},
                    )
                logger.info(f"http_webhook ok: {url} status={resp.status_code} attempts={attempts}")
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"status_code": resp.status_code, "response": truncated, "attempts": attempts},
                    summary=f"HTTP {resp.status_code} (attempts={attempts})",
                )
            except Exception as e:
                last_err = str(e)
                if attempts <= retry:
                    logger.warning(f"http_webhook network error retry {attempts}/{retry}: {e}")
                    await asyncio.sleep(backoff)
        return NodeResult(
            status=NodeStatus.FAILED, error=f"Still failing after {retry} retries: {last_err}", data={"attempts": attempts}
        )


@register_node
class ApprovalGateNode(BaseNode):
    """Human decision gate — carried by the platform approval inbox, no terminal prompts.

    Approval-native: registered in HIGH_RISK_NODES -> permission layer denies by default -> HITL pause
    (#260 blocked steps visible) -> Web approval card / signed webhook callback decides -> resume.
    """

    name = "approval_gate"
    # i18n: user-facing
    display_name = "审批闸门"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "暂停流程等待人工决定; 审批走平台收件箱或签名 webhook 回调"
    icon = "🚧"
    # i18n: user-facing
    default_label = "审批闸门"

    # i18n: user-facing
    inputs = [{"key": "payload", "label": "待审内容", "type": "object"}]
    outputs = [
        # i18n: user-facing
        {"key": "approved", "label": "审批结论", "type": "boolean"},
        # i18n: user-facing
        {"key": "payload", "label": "透传内容", "type": "object"},
        # i18n: user-facing
        {"key": "human_reply", "label": "人工回复", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "prompt": {"type": "string", "description": "给审批人的说明"},
                "details": {"type": "string"},
                # issue #413: review-free gate selected by intake — false auto-passes the node, skips HITL
                "require_review": {
                    "type": "boolean",
                    "default": True,
                    # i18n: user-facing
                    "description": "false = 免评审 (intake review_arch=false), 节点自动通过记录 auto_passed",
                },
                # issue #508: node-level timeout policy — timeout_s overrides the engine default, on_timeout decides the timeout path
                "timeout_policy": {
                    "type": "object",
                    # i18n: user-facing
                    "description": "等待审批超时策略 {timeout_s: 秒, on_timeout: deny|fail}",
                    "properties": {
                        # i18n: user-facing
                        "timeout_s": {"type": "number", "description": "审批等待超时秒数 (覆盖引擎全局默认)"},
                        "on_timeout": {
                            "type": "string",
                            "enum": ["deny", "fail"],
                            # i18n: user-facing
                            "description": "deny = 合成拒绝走修订回路; fail (默认) = 分支直接失败终态",
                        },
                    },
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        prompt = str(params.get("prompt") or "")
        details = str(params.get("details") or "")
        require_review = params.get("require_review", True)
        if isinstance(require_review, str):
            require_review = require_review.strip().lower() not in ("false", "0", "no", "off")
        if not require_review:
            # issue #413: review-free gate auto-pass — engine side also skips permission/HITL checks (workflow.py)
            logger.info(f"approval_gate auto-pass (require_review=false): {prompt[:80]}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "approved": True,
                    "auto_passed": True,
                    "require_review": False,
                    "review_arch": False,
                    "payload": inputs.get("payload") or {},
                    "prompt": prompt,
                    "details": details,
                },
                summary=f"Review-free auto-pass (require_review=false): {prompt[:40]}",
            )
        # issue #309: per-item decision pass-through — batch_verdict merged into node inputs by the engine after mixed-decision wakeup
        batch = inputs.get("batch_verdict") or []
        if batch:
            rejected = [x for x in batch if not x.get("approved")]
            logger.info(
                f"approval_gate per-item decisions: {len(batch)} items, accepted {len(batch) - len(rejected)}, rejected {len(rejected)}"
            )
            summary = f"Human per-item decisions ({len(batch) - len(rejected)}/{len(batch)} accepted): {prompt[:40]}"
        else:
            rejected = []
            summary = f"Human approved: {prompt[:40]}"
            logger.info(f"approval_gate approved: {prompt[:80]}")
        # issue #508: structured human reply pass-through — downstream nodes read human_reply from the gate output
        human_reply = inputs.get("human_reply") or {}
        if isinstance(human_reply, dict) and human_reply:
            summary += f" +reply({len(human_reply)} items)"
            logger.info(f"approval_gate human reply pass-through: {sorted(human_reply)}")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "approved": True,
                "payload": inputs.get("payload") or {},
                "prompt": prompt,
                "details": details,
                "batch_verdict": batch,
                "rejected_items": rejected,
                "human_reply": human_reply if isinstance(human_reply, dict) else {},
            },
            summary=summary,
        )


# issue #358: strong-ref set for detached-session background watcher tasks — prevents GC, self-cleans on completion
_DETACHED_WATCHERS: set = set()


@register_node
class ClaudeDispatchNode(BaseNode):
    """issue #308: claude_dispatch — dispatch a short-lived coder session (claude -p) for a single task issue.

    Hard guardrails:
    - HIGH_RISK_NODES registration (sandbox evaluated per run)
    - token + wall-clock budget caps; over budget -> task issue back to state:ready + notify, never silently lost
    - session crash/timeout -> same fallback path
    - output must be delivered as a PR (result without PR URL = failure fallback, direct push to main forbidden)
    """

    name = "claude_dispatch"
    # i18n: user-facing
    display_name = "Coder 派发"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "派发 claude -p 短命会话处理任务 issue, 预算上限 + 崩溃/超时回退 state:ready, 产出必须为 PR"
    icon = "🚀"
    # i18n: user-facing
    default_label = "Coder 派发"

    # i18n: user-facing
    inputs = [{"key": "prompt", "label": "任务指令", "type": "string"}]
    outputs = [
        # i18n: user-facing
        {"key": "pr_url", "label": "PR 链接", "type": "string"},
        # i18n: user-facing
        {"key": "pr_number", "label": "PR 编号", "type": "number"},
        # i18n: user-facing
        {"key": "total_tokens", "label": "会话 token 用量", "type": "number"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "repo": {"type": "string", "description": "org/repo (回退标签操作目标)"},
                # i18n: user-facing
                "allowed_repos": {"type": "array", "items": {"type": "string"}, "description": "仓库白名单"},
                # i18n: user-facing
                "issue_number": {"type": "number", "description": "任务 issue 编号"},
                # i18n: user-facing
                "prompt": {"type": "string", "description": "任务指令 (会话 prompt 主体)"},
                "context_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    # i18n: user-facing
                    "description": "PRD/架构等上下文链接, 注入 prompt",
                },
                # i18n: user-facing
                "cwd": {"type": "string", "description": "脚手架仓库工作目录 (须已存在)"},
                # i18n: user-facing
                "claude_bin": {"type": "string", "default": "claude", "description": "claude 可执行文件路径"},
                "timeout": {
                    "type": "number",
                    "default": 3600,
                    # i18n: user-facing
                    "description": "墙钟上限 (秒, 缺省 3600, 可用 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 覆盖)",
                },
                # i18n: user-facing
                "max_tokens": {"type": "number", "default": 200000, "description": "token 用量上限"},
                "permission_mode": {
                    "type": "string",
                    "default": "bypassPermissions",
                    # i18n: user-facing
                    "description": (
                        # i18n: user-facing
                        "claude CLI --permission-mode (headless -p 下需审批的工具默认被自动拒绝, "
                        # i18n: user-facing
                        "自主派发默认 bypassPermissions; 补偿控制: allowed_repos 白名单 + 固定 cwd + PR 交付契约)"
                    ),
                },
                "ready_label": {"type": "string", "default": "state:ready"},
                "dispatched_label": {"type": "string", "default": "state:dispatched"},
                # issue #387: synthetic E2E — dry_run does not spawn a session, returns a deterministic canned PR
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "合成模式: 不派发 claude 会话, 返回确定性伪 PR (合成 E2E 管线用)",
                },
            },
            "required": ["repo", "allowed_repos", "issue_number", "prompt", "cwd"],
        }

    def _api_client(self, base_url: str, token: str, timeout: float) -> httpx.AsyncClient:
        # test seam: monkeypatch to inject MockTransport
        return httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=timeout,
        )

    def watchdog_floor(self) -> float:
        # issue #378: engine watchdog must never fire before this node's own dispatch
        # deadline — resolve the same chain execute() uses (param > env > 3600)
        try:
            raw = self.config.params.get("timeout") or os.environ.get("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT") or 3600
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    async def _issue_state(self, repo: str, issue_number: int) -> str:
        # issue #368: best-effort fetch of issue state for no-op delivery judgment; failure returns an empty string (follows the original failure path, no pass-through)
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token or not issue_number:
            return ""
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        try:
            async with self._api_client(base_url, token, 15.0) as client:
                issue = (await client.request("GET", f"/repos/{repo}/issues/{issue_number}")).json()
            return str((issue or {}).get("state") or "")
        except Exception as e:
            logger.info(f"claude_dispatch issue state fetch failed (treated as not closed): {e}")
            return ""

    async def _back_to_ready(self, repo: str, issue_number: int, ready_label: str, dispatched_label: str) -> bool:
        # best-effort label restore; failure is loud (WARN) and surfaces in node error data
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            logger.warning(f"claude_dispatch fallback failed: GITHUB_TOKEN not configured, issue #{issue_number} labels not restored")
            return False
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        try:
            async with self._api_client(base_url, token, 15.0) as client:
                r1 = await client.post(f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": [ready_label]})
                if dispatched_label:
                    await client.request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{dispatched_label}")
            ok = r1.status_code < 300
            if ok:
                logger.warning(f"claude_dispatch fallback: {repo}#{issue_number} labels restored {ready_label}")
            else:
                logger.warning(f"claude_dispatch fallback HTTP {r1.status_code}: {repo}#{issue_number}")
            return ok
        except Exception as e:
            logger.warning(f"claude_dispatch fallback error: {repo}#{issue_number} ({e})")
            return False

    @staticmethod
    def _extract_pr_url(text: str) -> str:
        m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text or "")
        return m.group(0) if m else ""

    @staticmethod
    def _extract_delivered_pr(text: str) -> str:
        # issue #381: session may declare an existing open PR as the delivery (retry
        # re-delivery) via `delivered_pr: <url>` in its result
        m = re.search(
            r'delivered_pr["\']?\s*[:=]\s*["\']?(https://github\.com/[\w.-]+/[\w.-]+/pull/\d+)',
            text or "",
            re.IGNORECASE,
        )
        return m.group(1) if m else ""

    async def _verify_linked_pr(self, repo: str, pr_url: str, issue_number: int) -> bool:
        # issue #381: linked delivery is valid only if the PR is open and its body
        # closes the task issue (same closing-link contract as GitHubNode #362);
        # any verification failure degrades to the strict fail path
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            logger.warning("claude_dispatch linked-PR verification skipped: GITHUB_TOKEN missing")
            return False
        pr_number = pr_url.rsplit("/", 1)[-1]
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        try:
            async with self._api_client(base_url, token, 15.0) as client:
                pr = (await client.request("GET", f"/repos/{repo}/pulls/{pr_number}")).json()
        except Exception as e:
            logger.warning(f"claude_dispatch linked-PR verification failed: {repo}#{pr_number} ({e})")
            return False
        if str((pr or {}).get("state") or "") != "open":
            return False
        body = str((pr or {}).get("body") or "")
        return bool(
            re.search(rf"\b(?:close[sd]?|fix(?:e[sd]?)?|resolve[sd]?)\s*:?\s*#{issue_number}\b", body, re.IGNORECASE)
        )

    def _spawn_detached_watcher(self, comm_task, proc, repo, issue_number, ready_label, dispatched_label) -> None:
        # issue #358: detach reaping — the same communicate task finishes in the background (no second communicate, no data loss)
        task = asyncio.create_task(
            self._watch_detached(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
        )
        _DETACHED_WATCHERS.add(task)
        task.add_done_callback(_DETACHED_WATCHERS.discard)

    async def _watch_detached(self, comm_task, proc, repo, issue_number, ready_label, dispatched_label) -> None:
        # issue #358: reap the detached session's end state — delivered PR is recorded for recovery; no PR falls back to state:ready, never silently lost
        try:
            stdout, _ = await comm_task
            output = stdout.decode("utf-8", "replace")
            pr_url = self._extract_pr_url(output)
            if proc.returncode == 0 and pr_url:
                logger.info(
                    f"claude_dispatch detached session delivered: {repo}#{issue_number} -> {pr_url} "
                    "(node already returned on timeout, PR needs manual linking or a pipeline re-run for acceptance)"
                )
                return
            head = output.strip()[:200]
            logger.warning(
                f"claude_dispatch detached session ended without PR: {repo}#{issue_number} exit={proc.returncode} output_head={head}"
            )
        except Exception as e:
            logger.warning(f"claude_dispatch detach watcher error: {repo}#{issue_number} ({e})")
        await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        repo = str(params.get("repo") or "")
        allowed = [str(x) for x in (params.get("allowed_repos") or [])]
        issue_number = int(params.get("issue_number") or 0)
        prompt = str(inputs.get("prompt") or params.get("prompt") or "")
        context_links = [str(x) for x in (params.get("context_links") or [])]
        cwd = str(params.get("cwd") or "")
        claude_bin = str(params.get("claude_bin") or "claude")
        # issue #358: a 1800s default would hard-kill real delivery sessions (implement+self-test+PR cycles often exceed 1800s) —
        # start at 3600s, globally adjustable via the FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT env var
        timeout = float(params.get("timeout") or os.environ.get("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT") or 3600)
        max_tokens = int(params.get("max_tokens") or 200000)
        # issue #353: approval-requiring tools are auto-denied under headless -p — autonomous dispatch defaults to bypassPermissions,
        # compensating controls: allowed_repos allowlist + fixed cwd + PR-only delivery contract + label state-machine fallback
        permission_mode = str(params.get("permission_mode") or "bypassPermissions")
        ready_label = str(params.get("ready_label") or "state:ready")
        dispatched_label = str(params.get("dispatched_label") or "state:dispatched")

        if repo not in allowed:
            logger.warning(f"claude_dispatch rejected: {repo} not in allowlist {allowed}")
            return NodeResult(status=NodeStatus.FAILED, error=f"Repository {repo} not in allowed_repos allowlist")
        if not issue_number or issue_number < 1:
            return NodeResult(status=NodeStatus.FAILED, error="issue_number must be a positive integer")
        if not prompt:
            return NodeResult(status=NodeStatus.FAILED, error="prompt must not be empty")
        if not cwd or not Path(cwd).is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"Working directory does not exist: {cwd}")

        # issue #387: synthetic E2E — validation still goes through allowlist/param checks, but no session is spawned, zero-cost deterministic canned PR
        dry_run = params.get("dry_run", False)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")
        if dry_run:
            pr_url = f"https://github.invalid/{repo}/pull/1"
            logger.info(f"claude_dispatch dry_run synthetic mode: {repo}#{issue_number} -> {pr_url}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "pr_url": pr_url,
                    "pr_number": 1,
                    "total_tokens": 0,
                    "dry_run": True,
                },
                summary=f"dry_run synthetic delivery: {repo}#{issue_number} -> {pr_url}",
            )

        full_prompt = prompt
        full_prompt += f"\n\nTask issue: {repo}#{issue_number}"
        if context_links:
            full_prompt += "\n\nContext docs:\n" + "\n".join(f"- {x}" for x in context_links)
        full_prompt += (
            "\n\nDelivery contract: open a pull request for your change. "
            "Never push directly to the default branch. "
            "Include self-test evidence in the PR description."
            # issue #381: retry re-delivery — an existing open PR covering the scope
            # is a valid delivery once it closes the task issue and is declared back
            "\n\nIf this task is a retry and its scope is already delivered by an existing open PR, "
            "do NOT open a duplicate PR: update that PR's body to reference this issue with "
            "'Closes #<issue number>', verify the work fresh, and include "
            "'delivered_pr: <pr url>' in your result."
        )

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "en_US.UTF-8",
        }
        # session needs repo push/PR rights
        if os.environ.get("GITHUB_TOKEN"):
            env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
        # issue #347: claude CLI auth variable pass-through — when the env allowlist lacks ANTHROPIC_AUTH_TOKEN,
        # the CLI exits 1 immediately as not logged in (guaranteed crash in daemon/launchd contexts without Keychain)
        for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            if os.environ.get(_k):
                env[_k] = os.environ[_k]

        argv = [claude_bin, "-p", full_prompt, "--output-format", "json", "--permission-mode", permission_mode]
        logger.info(
            f"claude_dispatch start: {repo}#{issue_number} cwd={cwd} timeout={timeout:g}s max_tokens={max_tokens}"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            return NodeResult(status=NodeStatus.FAILED, error=f"claude process launch failed: {e}")

        started = time.monotonic()
        # issue #358: communicate is handed to the task as a whole — on timeout the same task is handed to the watcher to keep reaping
        # (no second communicate, no data loss, the subprocess is never killed)
        comm_task = asyncio.create_task(proc.communicate())
        try:
            done, _pending = await asyncio.wait({comm_task}, timeout=timeout)
        except asyncio.CancelledError:
            # issue #358: the engine-level watchdog (#257) cancels this node before the node timeout — session is not killed,
            # handed to the watcher to reap the end state, avoiding an orphan process with no owner
            # issue #378: both timeouts in one frame — node deadline and engine cancellation recorded together
            logger.warning(
                f"claude_dispatch cancelled by watchdog, detach reaping: {repo}#{issue_number} pid={proc.pid} "
                f"node_deadline={timeout:g}s (session kept running)"
            )
            self._spawn_detached_watcher(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
            raise
        if comm_task not in done:
            elapsed = time.monotonic() - started
            # issue #358: timeout does not kill — detach semantics aligned with StudioRunNode (#351/#354):
            # hard-killing mid-delivery would discard sessions close to completion; the session keeps running, the watcher reaps the end state
            # (delivered PR is recorded, no PR falls back to state:ready); the label stays state:dispatched to prevent re-dispatch
            logger.warning(
                f"claude_dispatch timeout detach: {repo}#{issue_number} timeout={timeout:g}s "
                f"elapsed={elapsed:.1f}s pid={proc.pid} (session kept running, not killed)"
            )
            self._spawn_detached_watcher(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"claude session timed out ({timeout:g}s), detached (pid={proc.pid}, elapsed={elapsed:.0f}s): "
                    "session keeps running, end state reaped in the background (delivered PR recorded / no PR falls back to state:ready)"
                ),
                data={
                    "notification": f"task {repo}#{issue_number} dispatch timeout, session detached (pid {proc.pid})",
                    "pr_url": "",
                    "detached": True,
                    # issue #378: explicit outcome — session alive out-of-band, not a bare timeout
                    "outcome": "detached_session_still_running",
                    "pid": proc.pid,
                },
            )
        stdout, _ = comm_task.result()

        output = stdout.decode("utf-8", "replace")
        code = proc.returncode
        if code != 0:
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            # issue #347: session output head merged into the error — crashes are diagnosable without local reproduction
            head = output.strip()[:500]
            logger.warning(f"claude_dispatch session failed: {repo}#{issue_number} exit={code} output_head={head[:120]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"claude session exit code {code}, task sent back ({'state:ready restored' if restored else 'label restore failed, manual check needed'}): {head}",
                data={
                    "notification": f"task {repo}#{issue_number} dispatch crash (exit {code}), sent back to ready",
                    "output_head": head,
                },
            )

        # claude -p --output-format json: array of messages, final carries result + usage
        result_text, usage = "", {}
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "result" in obj:
                result_text = str(obj.get("result") or "")
                usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                break

        total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        pr_url = self._extract_pr_url(result_text)
        if total > max_tokens:
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch token budget exceeded: {repo}#{issue_number} {total}>{max_tokens}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"token budget exceeded ({total} > {max_tokens}), task sent back ({'state:ready restored' if restored else 'label restore failed'})",
                data={"notification": f"task {repo}#{issue_number} token budget exceeded ({total})", "pr_url": pr_url},
            )
        # issue #381: declared re-delivery takes precedence — session linked the task
        # issue to an existing open PR (Closes #N + delivered_pr declared) instead of
        # opening a duplicate; verified against the GitHub API, unverified falls
        # through to the strict path
        linked = self._extract_delivered_pr(result_text)
        if linked:
            if await self._verify_linked_pr(repo, linked, issue_number):
                logger.info(f"claude_dispatch linked delivery: {repo}#{issue_number} -> existing PR {linked}")
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={
                        "pr_url": linked,
                        "pr_number": int(linked.rsplit("/", 1)[-1]),
                        "total_tokens": total,
                        "session_claim": result_text.strip()[:500],
                        "outcome": "linked_existing_pr",
                    },
                    summary=f"{repo}#{issue_number} -> existing PR {linked} (linked delivery, tokens {total})",
                )
            logger.warning(
                f"claude_dispatch delivered_pr unverified (not open / not closing #{issue_number}): {linked}"
            )
        if linked:
            # declaration says "not a fresh PR" — never fall through to direct
            # delivery on the same URL; strict fail (no-op check still applies)
            state = await self._issue_state(repo, issue_number)
            if state == "closed":
                logger.info(
                    f"claude_dispatch no-op delivery: {repo}#{issue_number} issue already closed with no PR (duplicate/invalid disposition)"
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={
                        "pr_url": "",
                        "pr_number": 0,
                        "total_tokens": total,
                        "session_claim": result_text.strip()[:500],
                        "outcome": "closed_no_pr",
                    },
                    summary=f"{repo}#{issue_number} closed with no PR (no-op delivery, tokens {total})",
                )
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch linked-PR delivery failed verification: {repo}#{issue_number} {linked}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"delivered_pr failed verification ({linked}: must be open with body containing Closes #{issue_number}), "
                    f"task sent back ({'state:ready restored' if restored else 'label restore failed'})"
                ),
                data={
                    "notification": f"task {repo}#{issue_number} delivered_pr failed verification, sent back to ready",
                    "pr_url": linked,
                },
            )
        if not pr_url:
            # issue #368: session closing the issue as duplicate/invalid (with evidence) is a legitimate no-op delivery —
            # no state:ready fallback, no failure; state query failure (empty string) follows the original failure path
            state = await self._issue_state(repo, issue_number)
            if state == "closed":
                logger.info(
                    f"claude_dispatch no-op delivery: {repo}#{issue_number} issue already closed with no PR (duplicate/invalid disposition)"
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={
                        "pr_url": "",
                        "pr_number": 0,
                        "total_tokens": total,
                        "session_claim": result_text.strip()[:500],
                        "outcome": "closed_no_pr",
                    },
                    summary=f"{repo}#{issue_number} closed with no PR (no-op delivery, tokens {total})",
                )
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch produced no PR: {repo}#{issue_number}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Session produced no PR (delivery contract requires a PR, direct push to main forbidden), task sent back ({'state:ready restored' if restored else 'label restore failed'})",
                data={"notification": f"task {repo}#{issue_number} no PR delivered, sent back to ready"},
            )

        pr_number = int(pr_url.rsplit("/", 1)[-1])
        logger.info(f"claude_dispatch ok: {repo}#{issue_number} -> {pr_url} tokens={total}")
        # issue #362: session's self-declared delivery claim merged into the output — HITL review can distinguish delivery PRs from hygiene/aside PRs
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "pr_url": pr_url,
                "pr_number": pr_number,
                "total_tokens": total,
                "session_claim": result_text.strip()[:500],
            },
            summary=f"{repo}#{issue_number} -> {pr_url} (tokens {total})",
        )


@register_node
class RepoScaffoldNode(BaseNode):
    """issue #307: repo_scaffold — deterministic scaffold, generates a new business repo skeleton from architecture-doc conventions.

    Produces ~/fusion/business/ai-<slug>/: pyproject.toml / CLAUDE.md / README.md +
    README_CN.md / .python-version, git init, a port-registry hint file. Fails if the directory exists
    (never overwrites an existing repo).
    """

    name = "repo_scaffold"
    # i18n: user-facing
    display_name = "仓库脚手架"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "确定性生成 ai-<slug> 业务仓库骨架 (pyproject/CLAUDE.md/双语 README/.python-version) + git init"
    icon = "🏗️"
    # i18n: user-facing
    default_label = "仓库脚手架"

    # i18n: user-facing
    inputs = [{"key": "slug", "label": "仓库 slug", "type": "string"}]
    outputs = [
        # i18n: user-facing
        {"key": "repo_path", "label": "仓库路径", "type": "string"},
        # i18n: user-facing
        {"key": "created_files", "label": "创建文件清单", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "slug": {"type": "string", "description": "仓库 slug (^[a-z0-9][a-z0-9-]*$), 生成 ai-<slug>"},
                "base_dir": {
                    "type": "string",
                    "default": "~/fusion/business",
                    # i18n: user-facing
                    "description": "业务仓库根目录 (目录须已存在)",
                },
                # i18n: user-facing
                "display_name": {"type": "string", "description": "README 标题用显示名"},
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": (
                        # i18n: user-facing
                        "目标目录已存在且有内容时补齐缺失的脚手架文件 (issue #352 重跑幂等; "
                        # i18n: user-facing
                        "issue #368 非破坏性: 不覆写任何既有文件 — 既有文件视为交付/用户内容; 空目录始终自动复用)"
                    ),
                },
                # issue #422: extend path — on verdict=extend no new scaffold is generated; instead
                # clone the existing repo (extend_repo) and extend incrementally; tokens only via env vars
                "verdict": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "historical_match 判定 (extend/base/new; 非 extend 走正常脚手架)",
                },
                "extend_repo": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "extend 模式克隆的已有仓库 (owner/name; 缺省取上游输入 extend_repo)",
                },
            },
            "required": ["slug"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        slug = str(inputs.get("slug") or params.get("slug") or "").strip()
        base_dir = str(params.get("base_dir") or "~/fusion/business")
        display_name = str(params.get("display_name") or "")
        overwrite = bool(params.get("overwrite") or False)
        # issue #422: extend path — when the verdict is extend, clone the existing repo instead of building a new scaffold
        verdict = str(inputs.get("verdict") or params.get("verdict", "") or "")
        extend_repo = str(inputs.get("extend_repo") or params.get("extend_repo", "") or "").strip()

        if verdict == "extend" and extend_repo:
            return await self._extend_clone(base_dir, extend_repo)

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return NodeResult(status=NodeStatus.FAILED, error=f"slug must match ^[a-z0-9][a-z0-9-]*$: {slug!r}")
        # issue #416: intake-derived slugs already carry the ai- prefix — normalize first, then add the prefix once, preventing ai-ai- double-prefix directories
        base_slug = slug[3:] if slug.startswith("ai-") else slug
        base = Path(base_dir).expanduser()
        if not base.is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"base_dir does not exist: {base}")
        repo_path = base / f"ai-{base_slug}"
        # issue #352: re-run idempotency — empty directories are reused automatically; with content, overwrite=true fills in missing scaffold files
        # issue #368: overwrite is non-destructive — existing files are never overwritten (treated as delivery/user content;
        # rewriting would knock a delivered repo back to stub, causing uncommitted regressions), otherwise fail loudly with a remediation hint
        reused = False
        existed = repo_path.exists()
        if repo_path.exists():
            if not any(repo_path.iterdir()):
                reused = True
                logger.info(f"repo_scaffold reusing empty directory: {repo_path}")
            elif overwrite:
                logger.warning(f"repo_scaffold overwrite: filling in missing scaffold files (existing files not overwritten): {repo_path}")
            else:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"Target directory already exists: {repo_path} (re-run idempotency: pass overwrite=true to fill in missing scaffold files, "
                        "empty directories are reused automatically, or delete it manually and retry)"
                    ),
                )

        pkg = f"fusion_ai_{base_slug.replace('-', '_')}"
        title = display_name or f"ai-{base_slug}"
        files: Dict[str, str] = {
            "pyproject.toml": (
                f'[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
                f'[project]\nname = "{pkg}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n\n'
                f'[tool.setuptools.packages.find]\nwhere = ["src"]\n'
            ),
            "CLAUDE.md": (
                f"# {title}\n\n"
                f"Scaffolded by fusion-cowork repo_scaffold. Fill in project guidance:\n"
                f"build/test commands, architecture notes, conventions.\n\n"
                f"- Python >= 3.12, shared root venv at ~/fusion/.venv\n"
                f"- Code comments and log messages in English\n"
                f"- 4-space indentation, no docstrings, always include logging\n"
            ),
            "README.md": f"# {title}\n\nScaffolded by fusion-cowork repo_scaffold.\n",
            # i18n: user-facing
            "README_CN.md": f"# {title}\n\n由 fusion-cowork repo_scaffold 脚手架生成。\n",
            ".python-version": "3.12\n",
            "port_registry_entry.json": json.dumps(
                {
                    "service": f"ai-{base_slug}",
                    "requested_port": None,
                    "note": "register the real port in ~/fusion/architecture/port-registry.yaml before first serve",
                },
                indent=4,
            ),
            f"src/{pkg}/__init__.py": '__version__ = "0.1.0"\n',
        }
        created: List[str] = []
        skipped: List[str] = []
        try:
            # All three paths (reuse empty / fill in / fresh) land the scaffold files; issue #368: existing files are always skipped, never overwritten
            for rel, content in files.items():
                p = repo_path / rel
                if p.exists():
                    skipped.append(rel)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                created.append(rel)
            # Skip init for directories that already have .git (re-running git init is harmless, but saves a subprocess)
            if not (repo_path / ".git").exists():
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "init",
                    "-q",
                    str(repo_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
                if proc.returncode != 0:
                    logger.warning(
                        f"repo_scaffold git init exit code {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}"
                    )
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"Scaffold write failed: {e}")
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error="git init timed out (30s)")

        logger.info(f"repo_scaffold ok: {repo_path} ({len(created)} written, {len(skipped)} skipped, reused={reused})")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"repo_path": str(repo_path), "created_files": created, "skipped_files": skipped, "reused": reused},
            summary=f"Scaffold complete{' (reused existing directory)' if existed else ''}: {repo_path}",
        )

    async def _extend_clone(self, base_dir: str, extend_repo: str) -> NodeResult:
        # issue #422: extend path — clone the customer's existing repo for incremental extension (never creates a new repo/slug).
        # tokens only via env vars (GIT_CONFIG_* injects http.extraheader, never in argv, never in logs)
        base = Path(base_dir).expanduser()
        if not base.is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"base_dir does not exist: {base}")
        repo_name = extend_repo.rsplit("/", 1)[-1]
        repo_path = base / repo_name
        if repo_path.is_dir() and any(repo_path.iterdir()):
            logger.info(f"repo_scaffold extend reusing existing worktree: {repo_path}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"repo_path": str(repo_path), "extended": True, "extend_repo": extend_repo, "created_files": []},
                summary=f"extend reusing existing worktree: {repo_path}",
            )
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            return NodeResult(status=NodeStatus.FAILED, error="extend clone requires GITHUB_TOKEN (environment variable)")
        clone_url = f"https://github.com/{extend_repo}.git"
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            }
        )
        logger.info(f"repo_scaffold extend clone: {extend_repo} -> {repo_path}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "50",
                clone_url,
                str(repo_path),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"extend clone launch failed: {e}")
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error=f"extend clone timed out (300s): {extend_repo}")
        if proc.returncode != 0:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"extend clone failed ({proc.returncode}): {err.decode('utf-8', 'replace')[:200]}",
            )
        logger.info(f"repo_scaffold extend clone ok: {repo_path}")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"repo_path": str(repo_path), "extended": True, "extend_repo": extend_repo, "created_files": []},
            summary=f"extend clone complete: {repo_path}",
        )
