"""issue #262: 官方通用节点库第一组 — agent_exec / github_api / command_evidence / http_webhook / approval_gate。

设计约束 (issue #262):
- 全部配置 per-flow-instance 数据, 节点代码零业务常量
- 令牌只走环境变量 (repo 白名单 / ${ENV} 插值 / HMAC secret env), 绝不入 flow 定义
- 白名单不匹配大声拒绝 (fail-loud), 不静默放行
- approval_gate 天生审批: 靠 HIGH_RISK_NODES 注册进平台 HITL 闸门 (#260 blocked 步骤可见)
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

# 内置密钥形态 (capture 脱敏 + 输出兜底), 覆盖常见 token 家族
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
            logger.warning(f"脱敏正则无效, 跳过: {pat} ({e})")
    return text


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[截断]...\n{text[-limit:]}"


@register_node
class AgentExecNode(BaseNode):
    """外部无头 agent/CLI 进程执行 — 捕获退出码/输出, 崩溃结构化上报。

    进程生命周期不在此重实现 (issue #262 契约): 崩溃 = 节点 FAILED,
    重启策略由调用方以重跑节点/新 run 承接, 状态走外部存储。
    """

    name = "agent_exec"
    display_name = "Agent 执行"
    category = NodeCategory.TOOL
    description = "以 argv 启动外部无头 agent/CLI 进程, 捕获退出码与输出"
    icon = "🤖"
    default_label = "Agent 执行"

    inputs = [{"key": "command", "label": "命令 argv", "type": "list[string]"}]
    outputs = [
        {"key": "exit_code", "label": "退出码", "type": "number"},
        {"key": "output", "label": "输出 (尾段)", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "description": "argv 列表 (不经 shell)"},
                "cwd": {"type": "string", "description": "工作目录 (须已存在)"},
                "timeout": {"type": "number", "description": "秒", "default": 120},
                "env_keys": {"type": "array", "items": {"type": "string"}, "description": "透传的环境变量名白名单"},
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
            return NodeResult(status=NodeStatus.FAILED, error="command 须为非空字符串数组 (argv, 不经 shell)")
        if cwd:
            if not Path(cwd).is_dir():
                return NodeResult(status=NodeStatus.FAILED, error=f"工作目录不存在: {cwd}")
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
            return NodeResult(status=NodeStatus.FAILED, error=f"进程启动失败: {e}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"agent_exec 超时被杀: argv0={command[0]} timeout={timeout:g}s")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"agent 进程超时 ({timeout:g}s), 已终止", data={"exit_code": None}
            )
        output = _tail(stdout.decode("utf-8", "replace"), max_output)
        code = proc.returncode
        if code == 0:
            logger.info(f"agent_exec ok: argv0={command[0]} exit=0")
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"exit_code": 0, "output": output}, summary="agent 进程成功"
            )
        if code is not None and code < 0:
            logger.error(f"agent_exec 崩溃: argv0={command[0]} signal={-code}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent 进程崩溃 (signal={-code}); 重启策略: 重跑本节点即重启, 状态由外部存储承接",
                data={"exit_code": code, "output": output},
            )
        logger.warning(f"agent_exec 非零退出: argv0={command[0]} exit={code}")
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"agent 进程退出码 {code}",
            data={"exit_code": code, "output": output},
            summary=f"退出码 {code}",
        )


@register_node
class GitHubNode(BaseNode):
    """GitHub API 节点 — issue/PR/label 操作, org/repo 白名单 + 环境变量令牌。"""

    name = "github_api"
    display_name = "GitHub"
    category = NodeCategory.TOOL
    description = "GitHub issue/PR/label 操作; 仓库白名单 + GITHUB_TOKEN 环境变量鉴权"
    icon = "🐙"
    default_label = "GitHub"

    inputs = [
        {"key": "repo", "label": "仓库 (org/repo)", "type": "string"},
        {"key": "issue_number", "label": "Issue/PR 编号", "type": "number"},
    ]
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
                    "description": "操作",
                },
                "repo": {"type": "string", "description": "org/repo"},
                "allowed_repos": {"type": "array", "items": {"type": "string"}, "description": "仓库白名单"},
                "issue_number": {"type": "number", "description": "issue/PR 编号"},
                "require": {
                    "type": "boolean",
                    "default": False,
                    "description": "linked_pr 专用: 无关联 PR 时节点失败 (显式阻塞, 重跑即复查)",
                },
                "require_closing_link": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "linked_pr 专用: PR 仅在正文提及 issue (无 Closes/Fixes/Resolves 关闭关键词) 时节点失败 — "
                        "拦截 hygiene/aside PR 冒充交付 (issue #362)"
                    ),
                },
                "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number", "default": 30},
                # issue #387: 合成 E2E — dry_run 返回确定性伪结果, 零网络零 token
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "合成模式: 不调 GitHub API, 返回确定性伪结果 (合成 E2E 管线用)",
                },
            },
            "required": ["op", "repo", "allowed_repos"],
        }

    def _client(self, base_url: str, token: str, timeout: float) -> httpx.AsyncClient:
        # 测试缝: monkeypatch 本方法注入 MockTransport
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
        # issue #427: 空串标签过滤 — 模板按判定插值 extension 标签, 非扩展运行为空串
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
            return NodeResult(status=NodeStatus.FAILED, error=f"未知操作: {op}")
        if repo not in allowed:
            logger.warning(f"github_api 拒绝: {repo} 不在白名单 {allowed}")
            return NodeResult(status=NodeStatus.FAILED, error=f"仓库 {repo} 不在 allowed_repos 白名单内")
        # issue #387: 合成 E2E — 校验仍走白名单/操作合法, 但零网络返回确定性伪结果
        dry_run = params.get("dry_run", False)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")
        if dry_run:
            logger.info(f"github_api dry_run 合成模式: {op} {repo}#{issue_number or '-'}")
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
                summary=f"{op} {repo} (dry_run 合成)",
            )
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            return NodeResult(status=NodeStatus.FAILED, error="GITHUB_TOKEN 未配置 (令牌只走环境变量, 不入 flow 定义)")
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")

        method, path, payload = "GET", "", None
        if op == "issue_create":
            method, path, payload = "POST", f"/repos/{repo}/issues", {"title": title, "body": body, "labels": labels}
            if not title:
                return NodeResult(status=NodeStatus.FAILED, error="issue_create 缺少 title")
        elif op == "issue_comment":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="issue_comment 缺少 issue_number")
            method, path, payload = "POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body}
        elif op == "issue_set_labels":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="issue_set_labels 缺少 issue_number")
            method, path, payload = "PUT", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": labels}
        elif op == "pr_get":
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="pr_get 缺少 issue_number")
            method, path = "GET", f"/repos/{repo}/pulls/{issue_number}"
        elif op == "linked_pr":
            # issue #306: 关联 PR 前置检查 — 纯 REST 读 (零 LLM), G4 验收闸门的证据闸门。
            # timeline 含 cross-referenced 事件, source.issue 带 pull_request 即为关联 PR;
            # 找到 PR 时尽力取 CI 状态 (失败不阻塞, 置空)。
            if not issue_number:
                return NodeResult(status=NodeStatus.FAILED, error="linked_pr 缺少 issue_number")
            method, path = "GET", f"/repos/{repo}/issues/{issue_number}/timeline"
        elif op == "issue_list":
            # issue #283: list issues by label (governance batch fan-out intake)
            labels = ",".join(labels) if labels else ""
            query = f"?labels={labels}" if labels else ""
            method, path = "GET", f"/repos/{repo}/issues{query}"

        logger.info(f"github_api {op} {repo}#{issue_number or '-'}")
        try:
            async with self._client(base_url, token, timeout) as client:
                # issue #352: issue_create 幂等 — 先查同标题 issue, 命中即复用不重建
                # (管道重跑不再重复建任务 issue; 标题精确相等才算命中)
                # issue #368: 查全状态 (open+closed) — 已交付关闭的同标题 issue 也是重复,
                # 只查 open 会在 PR 合并关闭后重跑时再建一条假任务
                if op == "issue_create":
                    # issue #409: GitHub search q 上限 256 字符 — 长标题截断 (best-effort 去重,
                    # 非身份判定; 建 issue 用完整标题不受影响); 截断不吃掉转义反斜杠
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
                                f"github_api issue_create 幂等复用: {repo}#{item.get('number')} "
                                f"(同标题 issue 已存在, state={item.get('state')})"
                            )
                            return NodeResult(
                                status=NodeStatus.SUCCESS,
                                data={"result": dict(item), "deduplicated": True, "state": item.get("state") or ""},
                                summary=f"issue 已存在 ({item.get('state')}), 复用 #{item.get('number')}: {title[:60]}",
                            )

                # issue #382: 写操作 (POST/PUT) 经 outbox 回执 — 同 run 同节点同 op 重放
                # (崩溃恢复/重试) 不再重复写; 读操作不走回执
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
                        logger.info(f"github_api {op} {repo} outbox 回执重放, 跳过重复写")
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
            logger.warning(f"github_api 失败: {op} {repo}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"GitHub API 调用失败: {e}")
        result = data if isinstance(data, (dict, list)) else {}
        if isinstance(result, dict):
            result.pop("diff_url", None)
        if op == "linked_pr":
            # issue #306: 扫 timeline 事件找 cross-referenced 的关联 PR; require 语义 —
            # 无 PR 即失败 (显式 "先提 PR 再进验收"), 重跑节点即复查。
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
                # issue #362: 链接强度 — closing (PR 正文带 Closes/Fixes/Resolves #N) 才算
                # 交付性关联; mention (仅正文提及) 记 WARN, 可选 require_closing_link 阻塞
                link_strength = await self._pr_link_strength(base_url, token, timeout, repo, found_num, issue_number)
                if link_strength == "mention":
                    logger.warning(
                        f"github_api linked_pr: {repo}#{issue_number} PR#{found_num} 仅提及 issue "
                        "(无关闭关键词), pr_link_strength=mention"
                    )
            if bool(params.get("require")) and not found_url:
                # issue #368: issue 已被派发会话关闭 (duplicate/invalid 处置, 无 PR 交付)
                # 不算验收阻塞 — no-op 交付放行; 状态查询失败 (空串) 走原阻塞路径
                state = await self._issue_state(base_url, token, timeout, repo, issue_number)
                if state == "closed":
                    logger.info(f"github_api linked_pr: {repo}#{issue_number} 无 PR 但 issue 已关闭 (no-op 交付, 放行)")
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
                        summary=f"issue #{issue_number} 已关闭且无关联 PR (no-op 交付)",
                    )
                logger.warning(f"github_api linked_pr: {repo}#{issue_number} 无关联 PR (require=true, 阻塞)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="先提 PR 再进验收 (issue 无关联 PR, G4 不得自证)",
                    data={"found": False, "pr_url": "", "pr_number": 0, "ci_status": ""},
                    summary="无关联 PR, 阻塞验收",
                )
            if bool(params.get("require_closing_link")) and found_url and link_strength == "mention":
                logger.warning(
                    f"github_api linked_pr: {repo}#{issue_number} PR#{found_num} 无关闭链接 (require_closing_link=true, 阻塞)"
                )
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"PR#{found_num} 仅提及 issue 未以关闭关键词关联 (需 Closes/Fixes/Resolves #{issue_number} "
                        "在 PR 正文), 不计为交付 PR (issue #362)"
                    ),
                    data={
                        "found": True,
                        "pr_url": found_url,
                        "pr_number": found_num,
                        "ci_status": ci_status,
                        "pr_link_strength": link_strength,
                    },
                    summary="PR 无关闭链接, 阻塞验收",
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
                summary=f"linked_pr {'✓ ' + found_url if found_url else '未关联 PR'}",
            )
        return NodeResult(status=NodeStatus.SUCCESS, data={"result": result}, summary=f"{op} {repo} ok")

    async def _pr_ci_status(self, base_url: str, token: str, timeout: float, repo: str, pr_number: int) -> str:
        # issue #306: 尽力取 PR CI 状态 (combined status); 任何失败返回空串不阻塞
        try:
            async with self._client(base_url, token, timeout) as client:
                pr = (await client.request("GET", f"/repos/{repo}/pulls/{pr_number}")).json()
                sha = str(((pr or {}).get("head") or {}).get("sha") or "")
                if not sha:
                    return ""
                st = (await client.request("GET", f"/repos/{repo}/commits/{sha}/status")).json()
                return str((st or {}).get("state") or "")
        except Exception as e:
            logger.info(f"github_api linked_pr: CI 状态获取失败 (忽略): {e}")
            return ""

    async def _pr_link_strength(
        self, base_url: str, token: str, timeout: float, repo: str, pr_number: int, issue_number: int
    ) -> str:
        # issue #362: closing = PR 正文带关闭关键词关联本 issue; mention = 仅提及; 失败降级 mention
        try:
            async with self._client(base_url, token, timeout) as client:
                pr = (await client.request("GET", f"/repos/{repo}/pulls/{pr_number}")).json()
                body = str((pr or {}).get("body") or "")
            pattern = rf"\b(?:close[sd]?|fix(?:e[sd]?)?|resolve[sd]?)\s*:?\s*#{issue_number}\b"
            return "closing" if re.search(pattern, body, re.IGNORECASE) else "mention"
        except Exception as e:
            logger.info(f"github_api linked_pr: PR 正文获取失败 (降级 mention): {e}")
            return "mention"

    async def _issue_state(self, base_url: str, token: str, timeout: float, repo: str, issue_number: int) -> str:
        # issue #368: 尽力取 issue 状态; 失败返回空串 (调用方走原阻塞路径, 不放行)
        try:
            async with self._client(base_url, token, timeout) as client:
                issue = (await client.request("GET", f"/repos/{repo}/issues/{issue_number}")).json()
            return str((issue or {}).get("state") or "")
        except Exception as e:
            logger.info(f"github_api linked_pr: issue 状态获取失败 (按未关闭处理): {e}")
            return ""


@register_node
class CommandEvidenceNode(BaseNode):
    """白名单命令执行 + 证据归档 — 端口对账 / 存活探测 / 密钥脱敏。"""

    name = "command_evidence"
    display_name = "命令取证"
    category = NodeCategory.TOOL
    description = "白名单命令执行, stdout/stderr 脱敏后归档证据目录, 附端口对账与存活探测"
    icon = "🧾"
    default_label = "命令取证"

    inputs = [{"key": "command", "label": "命令 argv", "type": "list[string]"}]
    outputs = [
        {"key": "exit_code", "label": "退出码", "type": "number"},
        {"key": "evidence_path", "label": "证据文件", "type": "string"},
        {"key": "ports", "label": "端口对账", "type": "list[object]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "description": "argv (不经 shell)"},
                "allowlist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "fnmatch 模式, 匹配 'argv 全拼' / argv0 basename / basename 去版本后缀 (python3.12 归一为 python)",
                },
                "evidence_dir": {"type": "string", "description": "证据归档目录"},
                "ports": {"type": "array", "items": {"type": "number"}, "description": "对账/探测端口"},
                "port_registry": {"type": "string", "description": "端口登记 JSON ({port: service}) 路径"},
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
            return NodeResult(status=NodeStatus.FAILED, error="command 须为非空字符串数组 (argv, 不经 shell)")
        if not allowlist:
            return NodeResult(status=NodeStatus.FAILED, error="allowlist 为空 — 拒绝执行 (fail-loud, 不放行)")
        # 白名单匹配三种形态: argv 全拼 (含绝对路径) + argv0 basename + basename 去版本后缀
        # (issue #334: venv/pyenv 解释器常为 python3.12/python3 — 与 `python *` 同族, 归一匹配)
        argv0_base = Path(command[0]).name
        argv0_family = argv0_base.rstrip(".0123456789")
        rest_joined = " ".join(str(c) for c in command[1:])
        candidates = (joined, " ".join([argv0_base, rest_joined]), " ".join([argv0_family, rest_joined]))
        if not any(fnmatch.fnmatch(cand, pat) for cand in candidates for pat in allowlist):
            logger.warning(f"command_evidence 拒绝白名单外命令: {joined}")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"命令不在白名单内, 已拒绝: {joined} (allowlist={allowlist})"
            )
        if not evidence_dir:
            return NodeResult(status=NodeStatus.FAILED, error="evidence_dir 必填")

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
                return NodeResult(status=NodeStatus.FAILED, error=f"命令超时 ({timeout:g}s), 已终止")
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"命令启动失败: {e}")

        raw = stdout.decode("utf-8", "replace")
        scrubbed = _scrub(raw, extra_redact)
        out_path = Path(evidence_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(command[0]).name}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = _scrub("$ " + " ".join(command), extra_redact)
        out_path.write_text(
            f"{header}\nexit={proc.returncode}\nat={time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n{scrubbed}\n",
            encoding="utf-8",
        )
        logger.info(f"command_evidence 归档: {out_path} exit={proc.returncode}")

        port_report: List[Dict[str, Any]] = []
        registry: Dict[str, Any] = {}
        if port_registry and Path(port_registry).is_file():
            try:
                registry = json.loads(Path(port_registry).read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"端口登记文件不可读 (忽略): {e}")
        for port in ports:
            live = await self._probe_port(port)
            expected = registry.get(str(port), registry.get(port))
            port_report.append({"port": port, "expected": expected, "live": live})
            if expected is not None and bool(live) != bool(expected):
                logger.warning(f"command_evidence 端口对账不一致: {port} 登记={expected} 实测存活={live}")

        output_tail = _tail(scrubbed, max_output)
        if proc.returncode == 0:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"exit_code": 0, "evidence_path": str(out_path), "ports": port_report, "output": output_tail},
                summary=f"exit=0, 证据已归档 ({len(port_report)} 端口对账)",
            )
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"命令退出码 {proc.returncode}",
            data={
                "exit_code": proc.returncode,
                "evidence_path": str(out_path),
                "ports": port_report,
                "output": output_tail,
            },
        )


@register_node
class HttpWebhookNode(BaseNode):
    """HTTP/Webhook 调用 — 重试策略 + 出站 HMAC 签名; ${ENV} 插值令牌不入 flow。"""

    name = "http_webhook"
    display_name = "HTTP Webhook"
    category = NodeCategory.TOOL
    description = "调用外部 HTTP API (重试 + HMAC 出站签名); 入站回调走 /api/hooks/webhook/{run_id} 续跑审批闸门"
    icon = "🔗"
    default_label = "HTTP Webhook"

    inputs = [{"key": "body", "label": "请求体", "type": "object"}]
    outputs = [
        {"key": "status_code", "label": "HTTP 状态码", "type": "number"},
        {"key": "response", "label": "响应 (截断)", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 目标"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT"], "default": "POST"},
                "headers": {"type": "object", "description": "值支持 ${ENV_NAME} 运行时插值"},
                "retry": {"type": "number", "default": 2, "description": "网络错误/5xx 重试次数"},
                "retry_backoff": {"type": "number", "default": 1.0},
                "timeout": {"type": "number", "default": 30},
                "sign_secret_env": {
                    "type": "string",
                    "default": "FUSION_COWORK_WEBHOOK_SECRET",
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
            raise RuntimeError(f"头 {env_name} 引用的环境变量为空 — 令牌只走环境变量")
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
                status=NodeStatus.FAILED, error=f"非法 URL scheme: {url.split(':')[0] if ':' in url else url}"
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
                    logger.warning(f"http_webhook 5xx ({resp.status_code}), 第 {attempts} 次重试前退避 {backoff:g}s")
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
                    logger.warning(f"http_webhook 网络错误重试 {attempts}/{retry}: {e}")
                    await asyncio.sleep(backoff)
        return NodeResult(
            status=NodeStatus.FAILED, error=f"重试 {retry} 次后仍失败: {last_err}", data={"attempts": attempts}
        )


@register_node
class ApprovalGateNode(BaseNode):
    """人工决策闸门 — 平台审批收件箱承载, 不做终端提示。

    天生审批: 注册进 HIGH_RISK_NODES → 权限层默认拒绝 → HITL 暂停
    (#260 blocked 步骤可见) → Web 审批卡 / 签名 webhook 回调决定 → 续跑。
    """

    name = "approval_gate"
    display_name = "审批闸门"
    category = NodeCategory.TOOL
    description = "暂停流程等待人工决定; 审批走平台收件箱或签名 webhook 回调"
    icon = "🚧"
    default_label = "审批闸门"

    inputs = [{"key": "payload", "label": "待审内容", "type": "object"}]
    outputs = [
        {"key": "approved", "label": "审批结论", "type": "boolean"},
        {"key": "payload", "label": "透传内容", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "给审批人的说明"},
                "details": {"type": "string"},
                # issue #413: intake 选择的免评审闸门 — false 时节点自动通过, 不进 HITL
                "require_review": {
                    "type": "boolean",
                    "default": True,
                    "description": "false = 免评审 (intake review_arch=false), 节点自动通过记录 auto_passed",
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
            # issue #413: 免评审闸门自动通过 — 引擎侧同步跳过权限/HITL 检查 (workflow.py)
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
                summary=f"免评审自动通过 (require_review=false): {prompt[:40]}",
            )
        # issue #309: 逐项决定透传 — batch_verdict 由引擎在混合决定唤醒后并入节点输入
        batch = inputs.get("batch_verdict") or []
        if batch:
            rejected = [x for x in batch if not x.get("approved")]
            logger.info(
                f"approval_gate 逐项决定: {len(batch)} 项, 接受 {len(batch) - len(rejected)}, 拒绝 {len(rejected)}"
            )
            summary = f"人工逐项决定 ({len(batch) - len(rejected)}/{len(batch)} 接受): {prompt[:40]}"
        else:
            rejected = []
            summary = f"人工批准: {prompt[:40]}"
            logger.info(f"approval_gate 通过: {prompt[:80]}")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "approved": True,
                "payload": inputs.get("payload") or {},
                "prompt": prompt,
                "details": details,
                "batch_verdict": batch,
                "rejected_items": rejected,
            },
            summary=summary,
        )


# issue #358: detach 会话的后台收割任务强引用集 — 防止任务被 GC, 完成后自清理
_DETACHED_WATCHERS: set = set()


@register_node
class ClaudeDispatchNode(BaseNode):
    """issue #308: claude_dispatch — 派发短命 coder 会话 (claude -p) 处理单个任务 issue。

    硬性护栏:
    - HIGH_RISK_NODES 注册 (sandbox 逐次评估)
    - token + 墙钟预算上限; 超限 -> 任务 issue 回 state:ready + 通知, 绝不静默丢失
    - 会话崩溃/超时 -> 同回退路径
    - 产出必须以 PR 交付 (结果无 PR URL = 失败回退, 禁止直推 main)
    """

    name = "claude_dispatch"
    display_name = "Coder 派发"
    category = NodeCategory.TOOL
    description = "派发 claude -p 短命会话处理任务 issue, 预算上限 + 崩溃/超时回退 state:ready, 产出必须为 PR"
    icon = "🚀"
    default_label = "Coder 派发"

    inputs = [{"key": "prompt", "label": "任务指令", "type": "string"}]
    outputs = [
        {"key": "pr_url", "label": "PR 链接", "type": "string"},
        {"key": "pr_number", "label": "PR 编号", "type": "number"},
        {"key": "total_tokens", "label": "会话 token 用量", "type": "number"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "org/repo (回退标签操作目标)"},
                "allowed_repos": {"type": "array", "items": {"type": "string"}, "description": "仓库白名单"},
                "issue_number": {"type": "number", "description": "任务 issue 编号"},
                "prompt": {"type": "string", "description": "任务指令 (会话 prompt 主体)"},
                "context_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "PRD/架构等上下文链接, 注入 prompt",
                },
                "cwd": {"type": "string", "description": "脚手架仓库工作目录 (须已存在)"},
                "claude_bin": {"type": "string", "default": "claude", "description": "claude 可执行文件路径"},
                "timeout": {
                    "type": "number",
                    "default": 3600,
                    "description": "墙钟上限 (秒, 缺省 3600, 可用 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 覆盖)",
                },
                "max_tokens": {"type": "number", "default": 200000, "description": "token 用量上限"},
                "permission_mode": {
                    "type": "string",
                    "default": "bypassPermissions",
                    "description": (
                        "claude CLI --permission-mode (headless -p 下需审批的工具默认被自动拒绝, "
                        "自主派发默认 bypassPermissions; 补偿控制: allowed_repos 白名单 + 固定 cwd + PR 交付契约)"
                    ),
                },
                "ready_label": {"type": "string", "default": "state:ready"},
                "dispatched_label": {"type": "string", "default": "state:dispatched"},
                # issue #387: 合成 E2E — dry_run 不 spawn 会话, 返回确定性伪 PR
                "dry_run": {
                    "type": "boolean",
                    "default": False,
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
        # issue #368: 尽力取 issue 状态判定 no-op 交付; 失败返回空串 (走原失败路径, 不放行)
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token or not issue_number:
            return ""
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        try:
            async with self._api_client(base_url, token, 15.0) as client:
                issue = (await client.request("GET", f"/repos/{repo}/issues/{issue_number}")).json()
            return str((issue or {}).get("state") or "")
        except Exception as e:
            logger.info(f"claude_dispatch issue 状态获取失败 (按未关闭处理): {e}")
            return ""

    async def _back_to_ready(self, repo: str, issue_number: int, ready_label: str, dispatched_label: str) -> bool:
        # best-effort label restore; failure is loud (WARN) and surfaces in node error data
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            logger.warning(f"claude_dispatch 回退失败: GITHUB_TOKEN 未配置, issue #{issue_number} 标签未恢复")
            return False
        base_url = os.environ.get("FUSION_GITHUB_API", "https://api.github.com").rstrip("/")
        try:
            async with self._api_client(base_url, token, 15.0) as client:
                r1 = await client.post(f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": [ready_label]})
                if dispatched_label:
                    await client.request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{dispatched_label}")
            ok = r1.status_code < 300
            if ok:
                logger.warning(f"claude_dispatch 回退: {repo}#{issue_number} 标签恢复 {ready_label}")
            else:
                logger.warning(f"claude_dispatch 回退 HTTP {r1.status_code}: {repo}#{issue_number}")
            return ok
        except Exception as e:
            logger.warning(f"claude_dispatch 回退异常: {repo}#{issue_number} ({e})")
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
        # issue #358: detach 收割 — 同一个 communicate 任务在后台跑完 (不二次 communicate, 不丢数据)
        task = asyncio.create_task(
            self._watch_detached(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
        )
        _DETACHED_WATCHERS.add(task)
        task.add_done_callback(_DETACHED_WATCHERS.discard)

    async def _watch_detached(self, comm_task, proc, repo, issue_number, ready_label, dispatched_label) -> None:
        # issue #358: 收割 detach 会话终局 — 交付 PR 则留痕可回收; 无 PR 则回退 state:ready, 绝不静默丢失
        try:
            stdout, _ = await comm_task
            output = stdout.decode("utf-8", "replace")
            pr_url = self._extract_pr_url(output)
            if proc.returncode == 0 and pr_url:
                logger.info(
                    f"claude_dispatch detach 会话交付: {repo}#{issue_number} -> {pr_url} "
                    "(节点已超时返回, PR 需人工关联或重跑管线验收)"
                )
                return
            head = output.strip()[:200]
            logger.warning(
                f"claude_dispatch detach 会话无 PR 终局: {repo}#{issue_number} exit={proc.returncode} output_head={head}"
            )
        except Exception as e:
            logger.warning(f"claude_dispatch detach watcher 异常: {repo}#{issue_number} ({e})")
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
        # issue #358: 缺省 1800s 会硬杀真实交付会话 (实现+自测+PR 周期普遍超 1800s) —
        # 3600s 起步, 可用 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 环境变量全局调整
        timeout = float(params.get("timeout") or os.environ.get("FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT") or 3600)
        max_tokens = int(params.get("max_tokens") or 200000)
        # issue #353: headless -p 下需审批工具被自动拒绝 — 自主派发默认 bypassPermissions,
        # 补偿控制: allowed_repos 白名单 + 固定 cwd + PR-only 交付契约 + 标签状态机回退
        permission_mode = str(params.get("permission_mode") or "bypassPermissions")
        ready_label = str(params.get("ready_label") or "state:ready")
        dispatched_label = str(params.get("dispatched_label") or "state:dispatched")

        if repo not in allowed:
            logger.warning(f"claude_dispatch 拒绝: {repo} 不在白名单 {allowed}")
            return NodeResult(status=NodeStatus.FAILED, error=f"仓库 {repo} 不在 allowed_repos 白名单内")
        if not issue_number or issue_number < 1:
            return NodeResult(status=NodeStatus.FAILED, error="issue_number 须为正整数")
        if not prompt:
            return NodeResult(status=NodeStatus.FAILED, error="prompt 不能为空")
        if not cwd or not Path(cwd).is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"工作目录不存在: {cwd}")

        # issue #387: 合成 E2E — 校验仍走白名单/参数完整, 但不 spawn 会话, 零成本确定性伪 PR
        dry_run = params.get("dry_run", False)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")
        if dry_run:
            pr_url = f"https://github.invalid/{repo}/pull/1"
            logger.info(f"claude_dispatch dry_run 合成模式: {repo}#{issue_number} -> {pr_url}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "pr_url": pr_url,
                    "pr_number": 1,
                    "total_tokens": 0,
                    "dry_run": True,
                },
                summary=f"dry_run 合成交付: {repo}#{issue_number} -> {pr_url}",
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
        # issue #347: claude CLI 认证变量透传 — env 白名单缺 ANTHROPIC_AUTH_TOKEN 时
        # CLI 未登录直接退出 1 (daemon/launchd 上下文无 Keychain 更是必挂)
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
            return NodeResult(status=NodeStatus.FAILED, error=f"claude 进程启动失败: {e}")

        started = time.monotonic()
        # issue #358: communicate 整体交给任务 — 超时时同一任务移交 watcher 继续收割
        # (不二次 communicate 不丢数据, 子进程绝不 kill)
        comm_task = asyncio.create_task(proc.communicate())
        try:
            done, _pending = await asyncio.wait({comm_task}, timeout=timeout)
        except asyncio.CancelledError:
            # issue #358: 引擎级看门狗 (#257) 先于节点超时取消本节点 — 会话不杀,
            # 移交 watcher 收割终局, 避免孤儿进程无归属
            # issue #378: 双超时同框 — node deadline 与 engine 取消一起留痕
            logger.warning(
                f"claude_dispatch 被看门狗取消, detach 收割: {repo}#{issue_number} pid={proc.pid} "
                f"node_deadline={timeout:g}s (会话保留继续执行)"
            )
            self._spawn_detached_watcher(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
            raise
        if comm_task not in done:
            elapsed = time.monotonic() - started
            # issue #358: 超时不 kill — detach 语义与 StudioRunNode (#351/#354) 对齐:
            # 交付中段硬杀会丢弃接近完成的会话; 会话保留继续执行, watcher 收割终局
            # (交付 PR 则留痕, 无 PR 则回退 state:ready); 标签保持 state:dispatched 防重复派发
            logger.warning(
                f"claude_dispatch 超时 detach: {repo}#{issue_number} timeout={timeout:g}s "
                f"elapsed={elapsed:.1f}s pid={proc.pid} (会话保留继续执行, 不 kill)"
            )
            self._spawn_detached_watcher(comm_task, proc, repo, issue_number, ready_label, dispatched_label)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"claude 会话超时 ({timeout:g}s), 已 detach (pid={proc.pid}, elapsed={elapsed:.0f}s): "
                    "会话保留继续执行, 终局由后台收割 (交付 PR 留痕 / 无 PR 回退 state:ready)"
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
            # issue #347: 会话输出头并入错误 — 崩溃可诊断, 无需本地复现
            head = output.strip()[:500]
            logger.warning(f"claude_dispatch 会话失败: {repo}#{issue_number} exit={code} output_head={head[:120]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"claude 会话退出码 {code}, 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败, 需人工检查'}): {head}",
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
            logger.warning(f"claude_dispatch token 超限: {repo}#{issue_number} {total}>{max_tokens}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"token 预算超限 ({total} > {max_tokens}), 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败'})",
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
                    f"claude_dispatch no-op 交付: {repo}#{issue_number} issue 已关闭且无 PR (duplicate/invalid 处置)"
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
                    summary=f"{repo}#{issue_number} 已关闭且无 PR (no-op 交付, tokens {total})",
                )
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch linked-PR 交付未通过校验: {repo}#{issue_number} {linked}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"delivered_pr 校验未通过 ({linked}: 须 open 且 body 含 Closes #{issue_number}), "
                    f"任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败'})"
                ),
                data={
                    "notification": f"task {repo}#{issue_number} delivered_pr failed verification, sent back to ready",
                    "pr_url": linked,
                },
            )
        if not pr_url:
            # issue #368: 会话将 issue 关闭为 duplicate/invalid (带证据) 是合法 no-op 交付 —
            # 不回退 state:ready 不报失败; 状态查询失败 (空串) 走原失败路径
            state = await self._issue_state(repo, issue_number)
            if state == "closed":
                logger.info(
                    f"claude_dispatch no-op 交付: {repo}#{issue_number} issue 已关闭且无 PR (duplicate/invalid 处置)"
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
                    summary=f"{repo}#{issue_number} 已关闭且无 PR (no-op 交付, tokens {total})",
                )
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch 无 PR 产出: {repo}#{issue_number}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"会话未产出 PR (交付契约要求 PR, 禁止直推 main), 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败'})",
                data={"notification": f"task {repo}#{issue_number} no PR delivered, sent back to ready"},
            )

        pr_number = int(pr_url.rsplit("/", 1)[-1])
        logger.info(f"claude_dispatch ok: {repo}#{issue_number} -> {pr_url} tokens={total}")
        # issue #362: 会话自述交付声明并入输出 — HITL 评审可区分交付 PR 与 hygiene/aside PR
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
    """issue #307: repo_scaffold — 确定性脚手架, 从架构文档约定生成新业务仓库骨架。

    产出 ~/fusion/business/ai-<slug>/: pyproject.toml / CLAUDE.md / README.md +
    README_CN.md / .python-version, git init, 端口登记提示文件。目录已存在即失败
    (不覆盖既有仓库)。
    """

    name = "repo_scaffold"
    display_name = "仓库脚手架"
    category = NodeCategory.TOOL
    description = "确定性生成 ai-<slug> 业务仓库骨架 (pyproject/CLAUDE.md/双语 README/.python-version) + git init"
    icon = "🏗️"
    default_label = "仓库脚手架"

    inputs = [{"key": "slug", "label": "仓库 slug", "type": "string"}]
    outputs = [
        {"key": "repo_path", "label": "仓库路径", "type": "string"},
        {"key": "created_files", "label": "创建文件清单", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "仓库 slug (^[a-z0-9][a-z0-9-]*$), 生成 ai-<slug>"},
                "base_dir": {
                    "type": "string",
                    "default": "~/fusion/business",
                    "description": "业务仓库根目录 (目录须已存在)",
                },
                "display_name": {"type": "string", "description": "README 标题用显示名"},
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "目标目录已存在且有内容时补齐缺失的脚手架文件 (issue #352 重跑幂等; "
                        "issue #368 非破坏性: 不覆写任何既有文件 — 既有文件视为交付/用户内容; 空目录始终自动复用)"
                    ),
                },
                # issue #422: extend 路径 — verdict=extend 时不再生成新脚手架, 改为
                # clone 已有仓库 (extend_repo), 增量扩展; token 只走环境变量
                "verdict": {
                    "type": "string",
                    "description": "historical_match 判定 (extend/base/new; 非 extend 走正常脚手架)",
                },
                "extend_repo": {
                    "type": "string",
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
        # issue #422: extend 路径 — 判定 extend 时 clone 已有仓, 不建新脚手架
        verdict = str(inputs.get("verdict") or params.get("verdict", "") or "")
        extend_repo = str(inputs.get("extend_repo") or params.get("extend_repo", "") or "").strip()

        if verdict == "extend" and extend_repo:
            return await self._extend_clone(base_dir, extend_repo)

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return NodeResult(status=NodeStatus.FAILED, error=f"slug 须匹配 ^[a-z0-9][a-z0-9-]*$: {slug!r}")
        # issue #416: intake 派生的 slug 本就带 ai- 前缀 — 先归一化再统一加前缀, 杜绝 ai-ai- 双前缀目录
        base_slug = slug[3:] if slug.startswith("ai-") else slug
        base = Path(base_dir).expanduser()
        if not base.is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"base_dir 不存在: {base}")
        repo_path = base / f"ai-{base_slug}"
        # issue #352: 重跑幂等 — 空目录自动复用; 有内容时 overwrite=true 补齐缺失脚手架文件
        # issue #368: overwrite 非破坏性 — 既有文件一律不覆写 (视为交付/用户内容,
        # 复写会把已交付仓库打回 stub 造成未提交回退), 否则明确报错给补救提示
        reused = False
        existed = repo_path.exists()
        if repo_path.exists():
            if not any(repo_path.iterdir()):
                reused = True
                logger.info(f"repo_scaffold 复用空目录: {repo_path}")
            elif overwrite:
                logger.warning(f"repo_scaffold overwrite: 补齐缺失脚手架文件 (不覆写既有文件): {repo_path}")
            else:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"目标目录已存在: {repo_path} (重跑幂等: 传 overwrite=true 补齐缺失脚手架文件, "
                        "空目录会自动复用, 或手动删除后重试)"
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
            # 复用空目录/补齐/全新三种路径都落脚手架文件; issue #368: 既有文件一律跳过不覆写
            for rel, content in files.items():
                p = repo_path / rel
                if p.exists():
                    skipped.append(rel)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                created.append(rel)
            # 已有 .git 的目录跳过 init (git init 重复执行无害, 但省一次子进程)
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
                        f"repo_scaffold git init 退出码 {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}"
                    )
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"脚手架写入失败: {e}")
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error="git init 超时 (30s)")

        logger.info(f"repo_scaffold ok: {repo_path} ({len(created)} written, {len(skipped)} skipped, reused={reused})")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"repo_path": str(repo_path), "created_files": created, "skipped_files": skipped, "reused": reused},
            summary=f"脚手架完成{' (复用已有目录)' if existed else ''}: {repo_path}",
        )

    async def _extend_clone(self, base_dir: str, extend_repo: str) -> NodeResult:
        # issue #422: extend 路径 — clone 客户已有仓库做增量扩展 (绝不建新仓/新 slug)。
        # token 只走环境变量 (GIT_CONFIG_* 注入 http.extraHeader, 不进 argv 不进日志)
        base = Path(base_dir).expanduser()
        if not base.is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"base_dir 不存在: {base}")
        repo_name = extend_repo.rsplit("/", 1)[-1]
        repo_path = base / repo_name
        if repo_path.is_dir() and any(repo_path.iterdir()):
            logger.info(f"repo_scaffold extend 复用已有工作树: {repo_path}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"repo_path": str(repo_path), "extended": True, "extend_repo": extend_repo, "created_files": []},
                summary=f"extend 复用已有工作树: {repo_path}",
            )
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            return NodeResult(status=NodeStatus.FAILED, error="extend clone 需要 GITHUB_TOKEN (环境变量)")
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
            return NodeResult(status=NodeStatus.FAILED, error=f"extend clone 启动失败: {e}")
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error=f"extend clone 超时 (300s): {extend_repo}")
        if proc.returncode != 0:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"extend clone 失败 ({proc.returncode}): {err.decode('utf-8', 'replace')[:200]}",
            )
        logger.info(f"repo_scaffold extend clone ok: {repo_path}")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"repo_path": str(repo_path), "extended": True, "extend_repo": extend_repo, "created_files": []},
            summary=f"extend 克隆完成: {repo_path}",
        )
