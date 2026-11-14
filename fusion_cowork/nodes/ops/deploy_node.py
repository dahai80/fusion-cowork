"""issue #489 Part 2: deploy_service — deployment executor node with healthcheck and app endpoint.

Executes a deployment for a delivered app and drives it to a running, health-checked
endpoint, consumable by the site deployment GUI (stage chain + log excerpts + retry):

- target="docker" (default): docker pull <image> -> docker run -d --name <name> -> poll healthcheck_url
- target="launchd": validate plist -> launchctl bootstrap gui/<uid> <plist> (fallback load) -> poll healthcheck_url
- target="script": run install_script via shell -> poll healthcheck_url

Stage chain recorded per stage: {stage, status, attempts, duration_s, log} (log = combined
output tail, last log_tail lines). Each stage retries up to stage_retries times before the
node fails (fail visible, partial stage chain preserved in data). The running app endpoint
(healthcheck_url) is returned so an embedded acceptance preview can open it directly.

Seams for tests: _run_cmd (subprocess) and _poll_health (HTTP polling) are module-level
async functions, monkeypatched in tests. No network in CI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

_DEFAULT_HEALTH_TIMEOUT = 120.0
_DEFAULT_HEALTH_INTERVAL = 2.0
_DEFAULT_STAGE_RETRIES = 2
_DEFAULT_CMD_TIMEOUT = 600.0
_DEFAULT_LOG_TAIL = 40
_STAGES = ("pull", "start", "healthcheck")


async def _run_cmd(cmd: str, timeout: float = _DEFAULT_CMD_TIMEOUT) -> Tuple[int, str]:
    # R-2 pattern (same as tool_nodes): new session leader so the whole process
    # group can be killed on timeout; combined stdout+stderr for log excerpts
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, f"command timed out after {timeout:.0f}s: {cmd[:120]}"
    return proc.returncode or 0, (stdout or b"").decode("utf-8", errors="replace")


async def _poll_health(url: str, timeout_s: float, interval_s: float) -> Tuple[bool, str]:
    # Poll the app endpoint until HTTP 200 within the deadline; returns (ok, detail)
    import httpx

    deadline = time.monotonic() + max(timeout_s, 1.0)
    last_err = ""
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=min(interval_s * 2, 10.0)) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                return True, f"HTTP 200 ({url})"
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        await asyncio.sleep(max(interval_s, 0.1))
    return False, f"healthcheck deadline {timeout_s:.0f}s exceeded, last error: {last_err}"


def _tail(text: str, lines: int) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-max(lines, 1) :])


@register_node
class DeployServiceNode(BaseNode):
    """部署执行节点 — 拉起交付应用到健康检查通过的可访问端点 (issue #489)。"""

    name = "deploy_service"
    display_name = "应用部署"
    category = NodeCategory.TOOL
    description = (
        "Deploy a delivered app to a running health-checked endpoint "
        "(docker pull/run, launchd service, or install script) with stage "
        "status, log excerpts and retry; exposes the app endpoint for preview"
    )
    icon = "🚀"
    default_label = "应用部署"

    inputs = [
        {"key": "image", "label": "镜像/交付物", "type": "string"},
        {"key": "install_script", "label": "安装脚本", "type": "string"},
    ]
    outputs = [
        {"key": "deployed", "label": "部署结果", "type": "boolean"},
        {"key": "endpoint", "label": "应用端点", "type": "string"},
        {"key": "stages", "label": "阶段链", "type": "array"},
        {"key": "summary", "label": "部署摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["docker", "launchd", "script"],
                    "description": "部署方式: docker = pull+run 容器; launchd = macOS launchd 服务; script = 安装脚本",
                    "default": "docker",
                },
                "image": {"type": "string", "description": "docker 镜像 (target=docker 必填); 可由上游输入 image 提供"},
                "container_name": {"type": "string", "description": "docker 容器名 (缺省 fusion-deploy)"},
                "ports": {"type": "string", "description": "docker 端口映射 (如 8080:8080, 传给 docker run -p)"},
                "env": {
                    "type": "object",
                    "description": "docker 容器环境变量 (k=v, 传给 docker run -e)",
                    "additionalProperties": {"type": "string"},
                },
                "plist_path": {"type": "string", "description": "launchd plist 路径 (target=launchd 必填)"},
                "install_script": {
                    "type": "string",
                    "description": "安装/启动脚本 (target=script 必填); 可由上游输入 install_script 提供",
                },
                "healthcheck_url": {
                    "type": "string",
                    "description": "应用健康检查端点 (必填) — 部署成功后即为可访问的应用端点",
                },
                "healthcheck_timeout": {
                    "type": "number",
                    "description": f"健康检查轮询总时限秒数 (缺省 {_DEFAULT_HEALTH_TIMEOUT:.0f})",
                    "default": _DEFAULT_HEALTH_TIMEOUT,
                },
                "healthcheck_interval": {
                    "type": "number",
                    "description": f"健康检查轮询间隔秒数 (缺省 {_DEFAULT_HEALTH_INTERVAL:.0f})",
                    "default": _DEFAULT_HEALTH_INTERVAL,
                },
                "stage_retries": {
                    "type": "integer",
                    "description": f"每阶段失败重试次数 (缺省 {_DEFAULT_STAGE_RETRIES})",
                    "default": _DEFAULT_STAGE_RETRIES,
                },
                "cmd_timeout": {
                    "type": "number",
                    "description": f"单条命令超时秒数 (缺省 {_DEFAULT_CMD_TIMEOUT:.0f})",
                    "default": _DEFAULT_CMD_TIMEOUT,
                },
                "log_tail": {
                    "type": "integer",
                    "description": f"阶段日志保留行数 (缺省 {_DEFAULT_LOG_TAIL})",
                    "default": _DEFAULT_LOG_TAIL,
                },
            },
            "required": ["healthcheck_url"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        target = str(params.get("target") or "docker").strip().lower()
        if target not in ("docker", "launchd", "script"):
            return NodeResult(status=NodeStatus.FAILED, error=f"deploy_service target 非法: {target}")
        health_url = str(params.get("healthcheck_url") or "").strip()
        if not health_url:
            return NodeResult(status=NodeStatus.FAILED, error="deploy_service 参数 healthcheck_url 必填")
        if not health_url.startswith(("http://", "https://")):
            return NodeResult(status=NodeStatus.FAILED, error=f"healthcheck_url 必须 http(s): {health_url}")

        image = str(params.get("image") or inputs.get("image") or "").strip()
        install_script = str(params.get("install_script") or inputs.get("install_script") or "").strip()
        if target == "docker" and not image:
            return NodeResult(status=NodeStatus.FAILED, error="target=docker 需要 image (params 或上游输入)")
        if target == "launchd" and not str(params.get("plist_path") or "").strip():
            return NodeResult(status=NodeStatus.FAILED, error="target=launchd 需要 plist_path")
        if target == "script" and not install_script:
            return NodeResult(status=NodeStatus.FAILED, error="target=script 需要 install_script (params 或上游输入)")

        try:
            hc_timeout = float(params.get("healthcheck_timeout", _DEFAULT_HEALTH_TIMEOUT))
            hc_interval = float(params.get("healthcheck_interval", _DEFAULT_HEALTH_INTERVAL))
            stage_retries = max(int(params.get("stage_retries", _DEFAULT_STAGE_RETRIES)), 0)
            cmd_timeout = float(params.get("cmd_timeout", _DEFAULT_CMD_TIMEOUT))
            log_tail = max(int(params.get("log_tail", _DEFAULT_LOG_TAIL)), 1)
        except (TypeError, ValueError) as e:
            return NodeResult(status=NodeStatus.FAILED, error=f"deploy_service 参数类型非法: {e}")

        container_name = str(params.get("container_name") or "fusion-deploy").strip()
        logger.info(
            f"deploy_service 开始: target={target} image={image[:80]!r} endpoint={health_url} retries={stage_retries}"
        )

        stages: List[Dict[str, Any]] = []

        async def run_stage(stage: str, cmd: str) -> Optional[Dict[str, Any]]:
            # One deploy stage with retry; returns failure record or None on success
            attempts = 0
            while attempts <= stage_retries:
                attempts += 1
                t0 = time.monotonic()
                rc, out = await _run_cmd(cmd, timeout=cmd_timeout)
                dur = round(time.monotonic() - t0, 2)
                entry = {
                    "stage": stage,
                    "status": "ok" if rc == 0 else "failed",
                    "attempts": attempts,
                    "duration_s": dur,
                    "log": _tail(out, log_tail),
                }
                if rc == 0:
                    stages.append(entry)
                    logger.info(f"deploy_service 阶段完成: {stage} (第 {attempts} 次尝试, {dur}s)")
                    return None
                entry["error"] = f"exit={rc}"
                logger.warning(f"deploy_service 阶段失败: {stage} 第 {attempts} 次尝试 exit={rc}")
                if attempts <= stage_retries:
                    await asyncio.sleep(min(2.0 * attempts, 10.0))
            stages.append(entry)
            return entry

        if target == "docker":
            fail = await run_stage("pull", f"docker pull {shlex.quote(image)}")
            if not fail:
                env_flags = "".join(f" -e {shlex.quote(f'{k}={v}')}" for k, v in (params.get("env") or {}).items())
                port_flags = "".join(
                    f" -p {shlex.quote(p.strip())}" for p in str(params.get("ports") or "").split(",") if p.strip()
                )
                # Replace a same-name container from a previous deploy (idempotent redeploy)
                await _run_cmd(f"docker rm -f {shlex.quote(container_name)}", timeout=60.0)
                fail = await run_stage(
                    "start",
                    f"docker run -d --name {shlex.quote(container_name)}{env_flags}{port_flags} {shlex.quote(image)}",
                )
        elif target == "launchd":
            plist = str(params.get("plist_path") or "").strip()
            if not Path(plist).expanduser().is_file():
                stages.append({"stage": "pull", "status": "failed", "attempts": 1, "error": f"plist 不存在: {plist}"})
                fail = stages[-1]
            else:
                stages.append({"stage": "pull", "status": "ok", "attempts": 1, "log": f"plist: {plist}"})
                uid = os.getuid()
                fail = await run_stage(
                    "start",
                    f"launchctl bootstrap gui/{uid} {shlex.quote(plist)} 2>/dev/null || launchctl load {shlex.quote(plist)}",
                )
        else:
            fail = await run_stage("start", install_script)

        if fail is not None:
            summary = f"deploy_service 部署失败于 {fail['stage']} 阶段 (尝试 {fail.get('attempts', 1)} 次)"
            logger.error(f"{summary}; endpoint={health_url}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=summary,
                data={"deployed": False, "endpoint": health_url, "stages": stages, "summary": summary},
                summary=summary,
            )

        t0 = time.monotonic()
        ok, detail = await _poll_health(health_url, timeout_s=hc_timeout, interval_s=hc_interval)
        stages.append(
            {
                "stage": "healthcheck",
                "status": "ok" if ok else "failed",
                "attempts": 1,
                "duration_s": round(time.monotonic() - t0, 2),
                "log": detail,
            }
        )
        endpoint = health_url if ok else ""
        if not ok:
            summary = f"deploy_service 服务已启动但健康检查未通过: {detail}"
            logger.error(f"{summary}; stages={[(s['stage'], s['status']) for s in stages]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=summary,
                data={"deployed": False, "endpoint": "", "stages": stages, "summary": summary},
                summary=summary,
            )

        summary = f"deploy_service 部署成功: {target} 运行中, 端点 {endpoint}"
        logger.info(summary)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"deployed": True, "endpoint": endpoint, "stages": stages, "summary": summary},
            summary=summary,
        )
