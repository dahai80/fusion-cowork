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
                "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number", "default": 30},
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
        labels = [str(x) for x in (params.get("labels") or [])]
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
            if found_num:
                ci_status = await self._pr_ci_status(base_url, token, timeout, repo, found_num)
            if bool(params.get("require")) and not found_url:
                logger.warning(f"github_api linked_pr: {repo}#{issue_number} 无关联 PR (require=true, 阻塞)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="先提 PR 再进验收 (issue 无关联 PR, G4 不得自证)",
                    data={"found": False, "pr_url": "", "pr_number": 0, "ci_status": ""},
                    summary="无关联 PR, 阻塞验收",
                )
            logger.info(f"github_api linked_pr: {repo}#{issue_number} found={found_num or False} ci={ci_status or '-'}")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "result": {
                        "found": bool(found_url),
                        "pr_url": found_url,
                        "pr_number": found_num,
                        "ci_status": ci_status,
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
                    "description": "fnmatch 模式, 匹配 'argv 全拼'",
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
        # 白名单匹配两种形态: argv 全拼 (含绝对路径) + argv0 basename 形态, 都不中才拒
        base_joined = " ".join([Path(command[0]).name] + [str(c) for c in command[1:]])
        if not any(fnmatch.fnmatch(joined, pat) or fnmatch.fnmatch(base_joined, pat) for pat in allowlist):
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
            },
            "required": ["prompt"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        prompt = str(params.get("prompt") or "")
        details = str(params.get("details") or "")
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
                "timeout": {"type": "number", "default": 1800, "description": "墙钟上限 (秒)"},
                "max_tokens": {"type": "number", "default": 200000, "description": "token 用量上限"},
                "ready_label": {"type": "string", "default": "state:ready"},
                "dispatched_label": {"type": "string", "default": "state:dispatched"},
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
                    await client.request(
                        "DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{dispatched_label}"
                    )
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

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        repo = str(params.get("repo") or "")
        allowed = [str(x) for x in (params.get("allowed_repos") or [])]
        issue_number = int(params.get("issue_number") or 0)
        prompt = str(inputs.get("prompt") or params.get("prompt") or "")
        context_links = [str(x) for x in (params.get("context_links") or [])]
        cwd = str(params.get("cwd") or "")
        claude_bin = str(params.get("claude_bin") or "claude")
        timeout = float(params.get("timeout") or 1800)
        max_tokens = int(params.get("max_tokens") or 200000)
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

        full_prompt = prompt
        full_prompt += f"\n\nTask issue: {repo}#{issue_number}"
        if context_links:
            full_prompt += "\n\nContext docs:\n" + "\n".join(f"- {x}" for x in context_links)
        full_prompt += (
            "\n\nDelivery contract: open a pull request for your change. "
            "Never push directly to the default branch. "
            "Include self-test evidence in the PR description."
        )

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "en_US.UTF-8",
        }
        # session needs repo push/PR rights
        if os.environ.get("GITHUB_TOKEN"):
            env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]

        argv = [claude_bin, "-p", full_prompt, "--output-format", "json"]
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

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch 超时: {repo}#{issue_number} timeout={timeout:g}s")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"claude 会话超时 ({timeout:g}s), 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败, 需人工检查'})",
                data={"notification": f"task {repo}#{issue_number} dispatch timeout, sent back to ready", "pr_url": ""},
            )

        output = stdout.decode("utf-8", "replace")
        code = proc.returncode
        if code != 0:
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch 会话失败: {repo}#{issue_number} exit={code}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"claude 会话退出码 {code}, 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败, 需人工检查'})",
                data={"notification": f"task {repo}#{issue_number} dispatch crash (exit {code}), sent back to ready"},
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
        if not pr_url:
            restored = await self._back_to_ready(repo, issue_number, ready_label, dispatched_label)
            logger.warning(f"claude_dispatch 无 PR 产出: {repo}#{issue_number}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"会话未产出 PR (交付契约要求 PR, 禁止直推 main), 任务已回退 ({'state:ready 已恢复' if restored else '标签恢复失败'})",
                data={"notification": f"task {repo}#{issue_number} no PR delivered, sent back to ready"},
            )

        pr_number = int(pr_url.rsplit("/", 1)[-1])
        logger.info(f"claude_dispatch ok: {repo}#{issue_number} -> {pr_url} tokens={total}")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"pr_url": pr_url, "pr_number": pr_number, "total_tokens": total},
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
            },
            "required": ["slug"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        slug = str(inputs.get("slug") or params.get("slug") or "").strip()
        base_dir = str(params.get("base_dir") or "~/fusion/business")
        display_name = str(params.get("display_name") or "")

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return NodeResult(status=NodeStatus.FAILED, error=f"slug 须匹配 ^[a-z0-9][a-z0-9-]*$: {slug!r}")
        base = Path(base_dir).expanduser()
        if not base.is_dir():
            return NodeResult(status=NodeStatus.FAILED, error=f"base_dir 不存在: {base}")
        repo_path = base / f"ai-{slug}"
        if repo_path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"目标目录已存在, 不覆盖: {repo_path}")

        pkg = f"fusion_ai_{slug.replace('-', '_')}"
        title = display_name or f"ai-{slug}"
        files: Dict[str, str] = {
            "pyproject.toml": (
                f'[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
                f'[project]\nname = "{pkg}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n\n'
                f"[tool.setuptools.packages.find]\nwhere = [\"src\"]\n"
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
                    "service": f"ai-{slug}",
                    "requested_port": None,
                    "note": "register the real port in ~/fusion/architecture/port-registry.yaml before first serve",
                },
                indent=4,
            ),
            f"src/{pkg}/__init__.py": '__version__ = "0.1.0"\n',
        }
        created: List[str] = []
        try:
            for rel, content in files.items():
                p = repo_path / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                created.append(rel)
            proc = await asyncio.create_subprocess_exec(
                "git", "init", "-q", str(repo_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning(f"repo_scaffold git init 退出码 {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}")
        except OSError as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"脚手架写入失败: {e}")
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error="git init 超时 (30s)")

        logger.info(f"repo_scaffold ok: {repo_path} ({len(created)} files)")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"repo_path": str(repo_path), "created_files": created},
            summary=f"脚手架完成: {repo_path}",
        )
