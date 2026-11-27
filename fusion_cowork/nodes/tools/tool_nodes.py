"""General tool nodes — adopted from the Squish built-in toolset (13 built-in tool patterns).

All tool nodes execute through fusion-mlx or local capabilities, with no external service dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)
from ...security.kernel_sandbox import (
    ExecutionTier,
    blocked_result,
    sandbox_result,
)

logger = logging.getLogger(__name__)

# issue #524: tools each SandboxRunner implementation accepts. Anything
# else is fail-closed refused (blocked=True) — a kernel SandboxPolicy
# wired with these runners must never silently run an unmapped tool.
_SANDBOXABLE_SHELL_TOOLS = frozenset({"shell_exec", "bash", "shell", "run_terminal"})
_SANDBOXABLE_REPL_TOOLS = frozenset({"python_repl", "code_execute", "python"})


def _kill_process_group(proc: subprocess.Popen) -> None:
    """R-2: kill the entire process group (the group created by start_new_session) to prevent orphaned grandchild processes.

    proc.kill() only kills direct children; grandchildren spawned by children keep running as orphans. killpg sends SIGKILL to the whole group.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
        logger.debug(f"killed process group pgid={pgid} via killpg (pid={proc.pid})")
    except (ProcessLookupError, PermissionError, OSError) as e:
        logger.debug(f"killpg failed, falling back to proc.kill: {e}")
        try:
            proc.kill()
        except OSError:
            pass


def _atomic_write_text(path: Path, content: str) -> None:
    # E-4: atomic write — write the tmp file fully then os.replace, preventing a half-written file from a mid-write crash.
    # Write tmp in the same directory (same filesystem so replace is atomic); clean up tmp on failure.
    import tempfile

    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise


# Command sandbox — blacklist/whitelist
_SHELL_BLACKLIST = frozenset(
    {
        "rm -rf /",
        "mkfs",
        "dd if=",
        ":(){ :|:& };:",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "launchctl unload",
        "csrutil disable",
    }
)

_SHELL_BLACKLIST_PREFIXES = frozenset(
    {
        "rm -rf /",
        "rm -r /",
        "rm -f /",
    }
)

_PYTHON_BLACKLIST_IMPORTS = frozenset(
    {
        "ctypes",
        "multiprocessing",
        "subprocess",
        "socketserver",
        "http.server",
        "xmlrpc",
        "asyncio.subprocess",
        "os.system",
        "os.popen",
    }
)


def _check_shell_command(command: str) -> Optional[str]:
    cmd_lower = command.strip().lower()
    for blocked in _SHELL_BLACKLIST:
        if blocked in cmd_lower:
            return f"命令被沙箱阻止: 含有禁止模式 '{blocked}'"  # i18n: user-facing
    for prefix in _SHELL_BLACKLIST_PREFIXES:
        if cmd_lower.startswith(prefix):
            return f"命令被沙箱阻止: 匹配禁止前缀 '{prefix}'"  # i18n: user-facing
    return None


# issue #319: shell operators change semantics when shlex.split'd (shell=False) —
# "echo a > f && cat f" becomes literal argv, silently faking success. Detect and
# fail loud so the caller either simplifies the command or opts into shell=True.
_SHELL_METACHARS = frozenset("|&;<>()$\n`")


def _check_shell_metachars(command: str) -> Optional[str]:
    for ch in _SHELL_METACHARS:
        if ch in command:
            return (
                f"command contains shell operator '{ch}' but shell=false — "
                "it would be passed as a literal argument, not executed; "
                "simplify the command or set shell=true"
            )
    return None


# issue #319: FUSION_COWORK_WORKSPACE_ROOT confinement — when set, shell_exec
# workdir defaults to the workspace root and any workdir must resolve inside it.
def _confine_workdir(workdir: str) -> tuple[Optional[str], Optional[str]]:
    root_raw = os.environ.get("FUSION_COWORK_WORKSPACE_ROOT", "")
    if not root_raw:
        return workdir, None
    try:
        root = Path(root_raw).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"workspace root unavailable: {root_raw} ({e})"
    wd = Path(workdir).expanduser() if workdir else root
    if not wd.is_absolute():
        wd = root / wd
    try:
        wd = wd.resolve(strict=False)
    except OSError as e:
        return None, f"workdir resolve failed: {workdir} ({e})"
    if wd != root:
        try:
            wd.relative_to(root)
        except ValueError:
            return None, f"workdir escapes workspace root: {workdir} (root: {root})"
    # Subdirectories may be auto-created — already ensured to be inside the root fence
    if not wd.exists():
        try:
            wd.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return None, f"workdir create failed: {wd} ({e})"
    return str(wd), None


# E-11: SSRF defense — reject private/loopback/link-local/cloud-metadata addresses
_SSRF_BANNED_HOSTS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",  # GCP metadata
        "169.254.169.254",  # AWS/Azure metadata
        "fd00.169.254.169.254",
    }
)


def _check_ssrf_url(url: str) -> Optional[str]:
    """Resolve the URL host and resolve all A/AAAA records; if any lands in private/loopback/link-local → reject.

    Returns the rejection reason str, or None when it passes. Resolution failure (malformed host) → reject (conservative).
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return "URL 无主机名"  # i18n: user-facing
    host_lower = host.lower().rstrip(".")
    if host_lower in _SSRF_BANNED_HOSTS:
        return f"主机 '{host}' 在 SSRF 黑名单 (内网/元数据)"  # i18n: user-facing

    # Literal IP: judged directly
    try:
        ip = ipaddress.ip_address(host_lower)
    except ValueError:
        ip = None
    if ip is not None and _is_private_ip(ip):
        return f"URL 主机 {ip} 属内网/环回/链路本地地址"  # i18n: user-facing

    # Domain: resolve all addresses, any private → reject (guards against DNS rebinding + split-horizon)
    if ip is None:
        try:
            infos = socket.getaddrinfo(host_lower, None)
        except OSError as e:
            return f"主机 '{host}' DNS 解析失败: {e}"  # i18n: user-facing
        seen = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _is_private_ip(resolved):
                return f"主机 '{host}' 解析到内网地址 {resolved} (SSRF 拒绝)"  # i18n: user-facing
    return None


def _is_private_ip(ip) -> bool:
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _check_python_code(code: str) -> Optional[str]:
    # MD-18: AST traversal replaces the regex blacklist (regex can be bypassed via comments/strings/unicode)
    # Parse failure (syntax error) still passes through to be reported by the subprocess; only dangerous constructs are rejected
    # A-4 hardening: intercept dynamic import/execution bypasses such as __import__/importlib/getattr(__builtins__)/eval/exec
    import ast

    _DANGEROUS_IMPORTS = _PYTHON_BLACKLIST_IMPORTS
    _DANGEROUS_ATTRS = {
        ("os", "system"),
        ("os", "popen"),
    }
    _DANGEROUS_ATTR_SUBSTR = ("subprocess",)
    # A-4: dangerous builtin names — __import__ dynamic import / importlib dynamic loading / eval·exec dynamic execution /
    # getattr(__builtins__, ...) fetching dangerous builtins / direct __builtins__ access
    _DANGEROUS_BUILTIN_CALLS = frozenset(
        {"__import__", "eval", "exec", "compile", "breakpoint", "globals", "locals", "vars"}
    )
    _DANGEROUS_NAMES = frozenset({"__import__", "importlib", "builtins", "__builtins__"})
    _DANGEROUS_ATTR_NAMES = frozenset({"__import__", "import_module", "exec", "eval", "compile"})
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Syntax errors are reported by the subprocess, not intercepted here (avoids false positives)
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == m or alias.name.startswith(m + ".") for m in _DANGEROUS_IMPORTS):
                    return f"代码被沙箱阻止: 禁止导入 '{alias.name}'"  # i18n: user-facing
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod == m or mod.startswith(m + ".") for m in _DANGEROUS_IMPORTS):
                return f"代码被沙箱阻止: 禁止 from '{mod}' 导入"  # i18n: user-facing
        elif isinstance(node, ast.Call):
            func = node.func
            # A-4: __import__("subprocess") — Name directly calling a dangerous builtin
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTIN_CALLS:
                return f"代码被沙箱阻止: 禁止调用内置 '{func.id}()' (可绕过静态导入拦截)"  # i18n: user-facing
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                attr_pair = (func.value.id, func.attr)
                if attr_pair in _DANGEROUS_ATTRS:
                    return f"代码被沙箱阻止: 禁止调用 {attr_pair[0]}.{attr_pair[1]}()"  # i18n: user-facing
                if func.value.id in _DANGEROUS_ATTR_SUBSTR:
                    return f"代码被沙箱阻止: 禁止使用 {func.value.id}.{func.attr}"  # i18n: user-facing
                # A-4: importlib.import_module(...) / builtins.__import__(...)
                if func.value.id in _DANGEROUS_NAMES or func.attr in _DANGEROUS_ATTR_NAMES:
                    return f"代码被沙箱阻止: 禁止动态导入/执行 {func.value.id}.{func.attr}()"  # i18n: user-facing
        # A-4: bare references to __import__ / importlib / __builtins__ used as values (e.g. assigned to a variable then called) are also rejected
        if isinstance(node, ast.Name) and node.id in _DANGEROUS_NAMES:
            ctx_is_load = isinstance(node.ctx, ast.Load)
            if ctx_is_load:
                return f"代码被沙箱阻止: 禁止引用 '{node.id}' (动态导入/执行逃逸面)"  # i18n: user-facing
        # A-4: getattr(__builtins__, "eval") / getattr(builtins, "system") — fetching dangerous objects via getattr
        if isinstance(node, ast.Call):
            f2 = node.func
            if isinstance(f2, ast.Name) and f2.id == "getattr" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in _DANGEROUS_NAMES:
                    return f"代码被沙箱阻止: 禁止 getattr({first.id}, ...) 取动态对象"  # i18n: user-facing
    return None


_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_repl_variables(variables: Dict[str, Any]) -> Optional[str]:
    # CR-13c: injected variable names must be valid Python identifiers; reject special characters (guards against script injection)
    for key in variables:
        if not isinstance(key, str) or not _IDENT_RE.match(key):
            return f"变量名非法 (非合法标识符): {key!r}"  # i18n: user-facing
        if key.startswith("__"):
            return f"变量名非法 (dunder 前缀): {key!r}"  # i18n: user-facing
    return None


@register_node
class ShellExecNode(BaseNode):
    """Shell command execution node — executes a local shell command and captures its output.

    Adopted from Squish's squish_run_shell tool.
    Executes safely via subprocess, supporting timeout and output capture.
    """

    name = "shell_exec"
    display_name = "Shell 命令执行"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "执行本地 shell 命令并获取输出"  # i18n: user-facing
    icon = "💻"
    default_label = "Shell 命令"  # i18n: user-facing

    # issue #524: honest tier — subprocess + static danger checks +
    # workspace fence, no seatbelt. Kernel SandboxRunner conformance
    # (additive): run_sandboxed() reuses the node's own execute() path.
    execution_tier = ExecutionTier.PROCESS

    def describe_sandbox_policy(self) -> Dict[str, Any]:
        return {
            "runner": self.name,
            "tier": self.execution_tier.value,
            "checks": [
                "static_danger_blacklist",
                "shell_metachars",
                "workspace_root_fence",
                "scoped_folder_manager",
                "process_group_killpg",
            ],
        }

    async def run_sandboxed(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name not in _SANDBOXABLE_SHELL_TOOLS:
            return blocked_result(
                tool_name, self.name, f"tool '{tool_name}' is not sandboxable by the shell_exec runner"
            )
        command = args.get("command") or args.get("cmd") or ""
        if not isinstance(command, str) or not command.strip():
            return blocked_result(tool_name, self.name, "missing non-empty 'command' string arg")
        result = await self.execute({"command": command})
        blocked = result.summary in (
            "沙箱拦截",  # i18n: user-facing
            "shell 元字符拒绝",
            "workspace root 围栏拦截",  # i18n: user-facing
            "沙箱拦截 workdir",
        )  # i18n: user-facing
        data = result.data or {}
        return sandbox_result(
            success=result.status == NodeStatus.SUCCESS,
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            return_code=data.get("return_code"),
            blocked=blocked,
            error=result.error or "",
            extra={"tool": tool_name, "command": command},
        )

    inputs = [
        {"key": "command", "label": "命令", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "stdout", "label": "标准输出", "type": "string"},  # i18n: user-facing
        {"key": "stderr", "label": "错误输出", "type": "string"},  # i18n: user-facing
        {"key": "return_code", "label": "返回码", "type": "integer"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",  # i18n: user-facing
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒）",  # i18n: user-facing
                    "default": 30,
                },
                "workdir": {
                    "type": "string",
                    "description": "工作目录",  # i18n: user-facing
                    "default": "",
                },
                "capture_output": {
                    "type": "boolean",
                    "description": "是否捕获输出",  # i18n: user-facing
                    "default": True,
                },
                "shell": {
                    "type": "boolean",
                    "description": "是否通过 shell 执行 (默认 False, 仅显式开启走 shell=True + WARNING)",  # i18n: user-facing
                    "default": False,
                },
            },
            "required": ["command"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        # Parameter coercion (adopted from Squish's _coerce_* mechanism)
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        command = inputs.get("command", params.get("command", ""))
        if not command:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="未指定命令",  # i18n: user-facing
                summary="未指定命令",  # i18n: user-facing
            )

        sandbox_error = _check_shell_command(command)
        if sandbox_error:
            logger.warning(f"ShellExec sandbox blocked: {command[:60]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=sandbox_error,
                summary="沙箱拦截",  # i18n: user-facing
            )

        timeout = params.get("timeout", 30)
        # issue #319: LLM-generated workflows often name it working_directory — adopted as a workdir alias
        workdir = params.get("workdir") or params.get("working_directory", "")
        capture = params.get("capture_output", True)
        use_shell = params.get("shell", False)

        # issue #319: workspace root fence — default to the root, reject out-of-bounds outright (fail-closed)
        workdir, confine_error = _confine_workdir(workdir)
        if confine_error:
            logger.error(f"ShellExec workdir escapes workspace root: {confine_error}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=confine_error,
                summary="workspace root 围栏拦截",  # i18n: user-facing
            )

        # issue #319: shell metachars become literal argv under shell=False — silently faking success; reject loudly up front
        if not use_shell:
            meta_error = _check_shell_metachars(command)
            if meta_error:
                logger.warning(f"ShellExec metachar rejection (shell=false): {command[:80]}")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=meta_error,
                    summary="shell 元字符拒绝",  # i18n: user-facing
                )

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if workdir and not scope.ensure_allowed(workdir):
            logger.warning(f"ShellExec out-of-bounds workdir rejected by sandbox: {workdir}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"沙箱拒绝越界工作目录: {workdir}",  # i18n: user-facing
                summary="沙箱拦截 workdir",  # i18n: user-facing
            )

        try:
            if use_shell:
                logger.warning(f"ShellExec shell=True (passed defense-in-depth blacklist): {command[:80]}")
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.PIPE if capture else None,
                    cwd=workdir or None,
                    # R-2: new session leader, the child forms its own process group, killpg can kill the whole group on timeout (incl. grandchildren)
                    start_new_session=True,
                )
            else:
                # CR-6/7: use_shell=False goes through an exec list (no /bin/sh), eliminating shell injection
                argv = shlex.split(command)
                if not argv:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error="命令解析为空",  # i18n: user-facing
                        summary="空命令",  # i18n: user-facing
                    )
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.PIPE if capture else None,
                    cwd=workdir or None,
                    # R-2: new session leader, killpg kills the whole group on timeout (old proc.kill only killed direct children, grandchildren orphaned)
                    start_new_session=True,
                )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                # R-2: killpg kills the entire process group (start_new_session forms its own group), preventing orphaned grandchildren
                _kill_process_group(proc)
                await proc.wait()
                logger.warning(f"ShellExec killed process group on timeout: {command[:80]} ({timeout}s)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"命令执行超时 ({timeout}s)",  # i18n: user-facing
                    data={"command": command, "timeout": timeout},
                    summary="执行超时",  # i18n: user-facing
                )

            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
            return_code = proc.returncode or 0

            status = NodeStatus.SUCCESS if return_code == 0 else NodeStatus.FAILED
            return NodeResult(
                status=status,
                data={
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "return_code": return_code,
                    "command": command,
                },
                summary=f"命令 {'成功' if return_code == 0 else '失败'} (返回码: {return_code})",  # i18n: user-facing
            )

        except FileNotFoundError as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"命令未找到: {e}",  # i18n: user-facing
                summary="命令未找到",  # i18n: user-facing
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"执行异常: {e}",  # i18n: user-facing
                summary="执行异常",  # i18n: user-facing
            )


@register_node
class PythonREPLNode(BaseNode):
    """Python REPL execution node — executes Python code snippets.

    Adopted from Squish's squish_python_repl tool.
    Executes in an isolated subprocess with limited resource usage.
    """

    name = "python_repl"
    display_name = "Python REPL"
    category = NodeCategory.TOOL
    description = "执行 Python 代码片段并返回结果"  # i18n: user-facing
    icon = "🐍"
    default_label = "Python 执行"  # i18n: user-facing

    # issue #524: honest tier — isolated subprocess + AST danger checks,
    # no seatbelt. Kernel SandboxRunner conformance (additive).
    execution_tier = ExecutionTier.PROCESS

    def describe_sandbox_policy(self) -> Dict[str, Any]:
        return {
            "runner": self.name,
            "tier": self.execution_tier.value,
            "checks": [
                "ast_danger_blacklist",
                "variable_identifier_validation",
                "subprocess_isolation",
                "process_group_killpg",
            ],
        }

    async def run_sandboxed(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name not in _SANDBOXABLE_REPL_TOOLS:
            return blocked_result(
                tool_name, self.name, f"tool '{tool_name}' is not sandboxable by the python_repl runner"
            )
        code = args.get("code") or args.get("script") or ""
        if not isinstance(code, str) or not code.strip():
            return blocked_result(tool_name, self.name, "missing non-empty 'code' string arg")
        result = await self.execute({"code": code})
        data = result.data or {}
        blocked = result.summary == "沙箱拦截"  # i18n: user-facing
        return sandbox_result(
            success=result.status == NodeStatus.SUCCESS,
            stdout=str(data.get("result", "")),
            stderr=str(data.get("stderr", "")),
            return_code=0 if result.status == NodeStatus.SUCCESS else 1,
            blocked=blocked,
            error=result.error or "",
            extra={"tool": tool_name, "result": data.get("result", ""), "type": data.get("type", "")},
        )

    inputs = [
        {"key": "code", "label": "Python 代码", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "result", "label": "执行结果", "type": "string"},  # i18n: user-facing
        {"key": "error", "label": "错误信息", "type": "string"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码",  # i18n: user-facing
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒）",  # i18n: user-facing
                    "default": 15,
                },
                "variables": {
                    "type": "object",
                    "description": "注入的变量（JSON 对象）",  # i18n: user-facing
                    "default": {},
                },
            },
            "required": ["code"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        code = inputs.get("code", params.get("code", ""))
        if not code:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="未指定 Python 代码",  # i18n: user-facing
                summary="未指定代码",  # i18n: user-facing
            )

        sandbox_error = _check_python_code(code)
        if sandbox_error:
            logger.warning(f"PythonREPL sandbox blocked: {code[:60]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=sandbox_error,
                summary="沙箱拦截",  # i18n: user-facing
            )

        timeout = params.get("timeout", 15)
        variables = params.get("variables", {})

        # CR-13c: variable-name validity check, rejects script injection
        var_error = _validate_repl_variables(variables)
        if var_error:
            logger.warning(f"PythonREPL variable-name validation blocked: {var_error}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=var_error,
                summary="沙箱拦截",  # i18n: user-facing
            )

        # Build the execution script
        script_lines = [
            "import json, sys, math, os, datetime, collections, itertools, random, re, pathlib",
            "",
        ]
        # Inject variables
        for key, value in variables.items():
            script_lines.append(f"{key} = {json.dumps(value)}")
        if variables:
            script_lines.append("")

        # issue #180: stdin read once into a variable — the old eval(sys.stdin.read()) drained the pipe first,
        # then the SyntaxError branch read an empty string from stdin, so multi-statement code silently did not run yet reported success
        script_lines.extend(
            [
                "_src = sys.stdin.read()",
                "try:",
                "    _result = eval(compile(_src, '<user_code>', 'eval'))",
                "    print(json.dumps({'result': repr(_result), 'type': type(_result).__name__}))",
                "except SyntaxError:",
                "    try:",
                "        exec(compile(_src, '<user_code>', 'exec'))",
                "        print(json.dumps({'result': 'executed', 'type': 'NoneType'}))",
                "    except Exception as e:",
                "        print(json.dumps({'error': str(e), 'type': type(e).__name__}))",
                "except Exception as e:",
                "    print(json.dumps({'error': str(e), 'type': type(e).__name__}))",
            ]
        )

        script = "\n".join(script_lines)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # R-2: new session leader, killpg kills the whole group on timeout (old proc.kill only killed direct children, orphaned grandchildren)
                start_new_session=True,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=code.encode("utf-8")),
                    timeout=timeout,
                )
            except TimeoutError:
                _kill_process_group(proc)
                await proc.wait()
                logger.warning(f"PythonREPL killed process group on timeout ({timeout}s)")
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"Python 执行超时 ({timeout}s)",  # i18n: user-facing
                    summary="执行超时",  # i18n: user-facing
                )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            # Parse the result
            try:
                result_data = json.loads(stdout_str) if stdout_str else {}
            except json.JSONDecodeError:
                result_data = {"result": stdout_str, "type": "str"}

            if "error" in result_data:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=result_data["error"],
                    data={"result": "", "error": result_data["error"], "stderr": stderr_str},
                    summary="执行错误",  # i18n: user-facing
                )

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "result": result_data.get("result", stdout_str),
                    "type": result_data.get("type", "str"),
                    "stderr": stderr_str,
                },
                summary=f"执行成功: {result_data.get('result', '')[:100]}",  # i18n: user-facing
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Python REPL 异常: {e}",  # i18n: user-facing
                summary="执行异常",  # i18n: user-facing
            )


class PythonREPLCodeExecutor:
    """issue #524: kernel CodeExecutor backend driving the python_repl
    subprocess semantics. harness code_mode (CodeModeTurn) never runs
    code itself — it calls this. Sync contract on purpose (matches the
    kernel protocol): a fresh subprocess per script, namespace injected
    as the exec globals, the code-mode `result` variable round-trips as
    the return value. Raises on failure so CodeModeTurn feeds the
    traceback back to the model for self-repair.
    """

    execution_tier = ExecutionTier.PROCESS

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def describe_sandbox_policy(self) -> Dict[str, Any]:
        return {
            "runner": "python_repl_code_executor",
            "tier": self.execution_tier.value,
            "checks": ["ast_danger_blacklist", "variable_identifier_validation", "subprocess_isolation"],
        }

    def execute(self, script: str, namespace: Dict[str, Any]) -> Any:
        if not isinstance(script, str) or not script.strip():
            raise ValueError("empty code-mode script")
        safety_error = _check_python_code(script)
        if safety_error:
            raise RuntimeError(f"Safety check failed: {safety_error}")
        ns = dict(namespace or {})
        var_error = _validate_repl_variables(ns)
        if var_error:
            raise ValueError(var_error)
        try:
            ns_payload = json.dumps(ns)
        except (TypeError, ValueError) as e:
            raise ValueError(f"code-mode namespace not JSON-serializable: {e}") from e

        wrapper = "\n".join(
            [
                "import contextlib, io, json, sys",
                "_data = sys.stdin.read()",
                "_nl = _data.find('\\n')",
                "_ns = json.loads(_data[:_nl]) if _nl >= 0 else {}",
                "_src = _data[_nl + 1:] if _nl >= 0 else ''",
                "_buf = io.StringIO()",
                "with contextlib.redirect_stdout(_buf):",
                "    exec(compile(_src, '<code_mode>', 'exec'), _ns)",
                "_out = _ns.get('result')",
                "try:",
                "    print(json.dumps({'ok': True, 'result': _out, 'stdout': _buf.getvalue()}))",
                "except (TypeError, ValueError):",
                "    print(json.dumps({'ok': True, 'result': repr(_out), 'stdout': _buf.getvalue()}))",
            ]
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", wrapper],
                input=f"{ns_payload}\n{script}",
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Python REPL execution timed out ({self.timeout}s)") from e
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "unknown REPL error").strip()[-2000:])
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"REPL returned non-JSON payload: {proc.stdout[:200]}") from e
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error", "unknown REPL error")))
        return payload.get("result")


@register_node
class WebSearchNode(BaseNode):
    """Web search node — searches web content.

    Adopted from Squish's squish_web_search tool.
    Implemented via the DuckDuckGo Lite endpoint; no API key required.
    """

    name = "web_search"
    display_name = "Web 搜索"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "搜索网页内容（通过 DuckDuckGo）"  # i18n: user-facing
    icon = "🌐"
    default_label = "Web 搜索"  # i18n: user-facing

    inputs = [
        {"key": "query", "label": "搜索关键词", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "results", "label": "搜索结果", "type": "list[dict]"},  # i18n: user-facing
        {"key": "result_count", "label": "结果数", "type": "integer"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},  # i18n: user-facing
                "max_results": {"type": "integer", "default": 5, "description": "最大结果数"},  # i18n: user-facing
                "timeout": {"type": "integer", "default": 10, "description": "超时秒数"},  # i18n: user-facing
            },
            "required": ["query"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        query = inputs.get("query", params.get("query", ""))
        if not query:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="未指定搜索关键词",  # i18n: user-facing
                summary="未指定搜索词",  # i18n: user-facing
            )

        max_results = params.get("max_results", 5)
        timeout = params.get("timeout", 10)

        import html as html_mod

        async def _ddg_search(client) -> list:
            # Uses the DuckDuckGo Lite API (original path)
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
            )
            resp.raise_for_status()

            import re

            html = resp.text
            link_pattern = re.compile(
                r'<a[^>]+class="result-link"[^>]+href="([^"]*)"[^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            snippet_pattern = re.compile(
                r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
                re.DOTALL | re.IGNORECASE,
            )

            links = link_pattern.findall(html)
            snippets = snippet_pattern.findall(html)

            results = []
            for i, (url, title) in enumerate(links[:max_results]):
                snippet = ""
                if i < len(snippets):
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i]).strip())
                results.append(
                    {
                        "title": html_mod.unescape(re.sub(r"<[^>]+>", "", title).strip()),
                        "url": url,
                        "snippet": snippet,
                    }
                )
            return results

        async def _bing_search(client) -> list:
            """Bing web results fallback — DDG is unreachable in some network environments."""
            import re

            resp = await client.get(
                "https://cn.bing.com/search",
                params={"q": query},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    )
                },
            )
            resp.raise_for_status()
            html = resp.text
            results = []
            for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S)[:max_results]:
                h2 = re.search(
                    r'<h2[^>]*><a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>',
                    block,
                    re.S,
                )
                if not h2:
                    continue
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", h2.group(2)).strip())
                snippet = ""
                p = re.search(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', block, re.S)
                if p:
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", p.group(1)).strip())
                results.append({"title": title, "url": h2.group(1), "snippet": snippet})
            return results

        try:
            import httpx

            results: list = []
            last_error = ""
            async with httpx.AsyncClient(timeout=timeout) as client:
                for engine in (_ddg_search, _bing_search):
                    try:
                        results = await engine(client)
                    except Exception as e:
                        last_error = str(e)
                        continue
                    if results:
                        break

            if not results and last_error:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"搜索失败: {last_error}",  # i18n: user-facing
                    summary="搜索失败",  # i18n: user-facing
                )

            return NodeResult(
                status=NodeStatus.SUCCESS if results else NodeStatus.FAILED,
                data={
                    "results": results,
                    "result_count": len(results),
                    "query": query,
                },
                summary=f"找到 {len(results)} 条结果",  # i18n: user-facing
            )

        except ImportError:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="需要 httpx 库: pip install httpx",  # i18n: user-facing
                summary="依赖缺失",  # i18n: user-facing
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"搜索失败: {e}",  # i18n: user-facing
                summary="搜索失败",  # i18n: user-facing
            )


@register_node
class FetchURLNode(BaseNode):
    """Web page fetch node — fetches URL content and returns text.

    Adopted from Squish's squish_fetch_url tool.
    Supports HTTP/HTTPS and automatically converts HTML to text.
    """

    name = "fetch_url"
    display_name = "获取网页"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "获取 URL 内容并返回文本"  # i18n: user-facing
    icon = "📄"
    default_label = "获取网页"  # i18n: user-facing

    inputs = [
        {"key": "url", "label": "URL", "type": "string"},
    ]
    outputs = [
        {"key": "content", "label": "内容", "type": "string"},  # i18n: user-facing
        {"key": "status_code", "label": "状态码", "type": "integer"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要获取的 URL"},  # i18n: user-facing
                "timeout": {"type": "integer", "default": 15, "description": "超时秒数"},  # i18n: user-facing
                "max_chars": {"type": "integer", "default": 100000, "description": "最大字符数"},  # i18n: user-facing
                "extract_text": {"type": "boolean", "default": True, "description": "提取纯文本"},  # i18n: user-facing
            },
            "required": ["url"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        url = inputs.get("url", params.get("url", ""))
        if not url:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="未指定 URL",  # i18n: user-facing
                summary="未指定 URL",  # i18n: user-facing
            )

        # Safety check (adopted from Squish's URL validation)
        if not url.startswith(("http://", "https://")):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"不安全的 URL 协议: {url.split('://')[0] if '://' in url else 'unknown'}://",  # i18n: user-facing
                summary="URL 协议不安全",  # i18n: user-facing
            )

        # E-11: SSRF defense — resolve the host, reject private/loopback/link-local/metadata addresses
        ssrf_err = _check_ssrf_url(url)
        if ssrf_err:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=ssrf_err,
                summary="URL 指向内网/元数据地址, 已拒 (SSRF 防御)",  # i18n: user-facing
            )

        timeout = params.get("timeout", 15)
        max_chars = params.get("max_chars", 100000)
        extract_text = params.get("extract_text", True)

        try:
            import httpx

            # E-11: follow_redirects=False — prevents redirects bypassing host validation (redirect targets are not SSRF-checked)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
                    },
                )
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")

                # Handle according to content type
                if "text/html" in content_type and extract_text:
                    import re

                    html = resp.text
                    # Simple HTML-to-text conversion
                    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    content = text[:max_chars]
                elif "application/json" in content_type:
                    content = json.dumps(resp.json(), ensure_ascii=False, indent=2)[:max_chars]
                else:
                    content = resp.text[:max_chars]

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "content": content,
                    "status_code": resp.status_code,
                    "content_type": content_type,
                    "url": url,
                },
                summary=f"获取成功 ({len(content)} 字符)",  # i18n: user-facing
            )

        except ImportError:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="需要 httpx 库: pip install httpx",  # i18n: user-facing
                summary="依赖缺失",  # i18n: user-facing
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"获取失败: {e}",  # i18n: user-facing
                summary="获取失败",  # i18n: user-facing
            )


@register_node
class ApplyEditNode(BaseNode):
    """File edit node — applies edit operations to a file (find and replace).

    Adopted from Squish's squish_apply_edit tool.
    Supports exact find-replace and regex replacement.
    """

    name = "apply_edit"
    display_name = "文件编辑"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "对文件应用编辑操作（查找替换）"  # i18n: user-facing
    icon = "✏️"
    default_label = "文件编辑"  # i18n: user-facing

    inputs = [
        {"key": "file_path", "label": "文件路径", "type": "string"},  # i18n: user-facing
        {"key": "old_text", "label": "查找文本", "type": "string"},  # i18n: user-facing
        {"key": "new_text", "label": "替换文本", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "success", "label": "是否成功", "type": "boolean"},  # i18n: user-facing
        {"key": "changes", "label": "变更数", "type": "integer"},  # i18n: user-facing
        {"key": "file_path", "label": "文件路径", "type": "string"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要编辑的文件路径"},  # i18n: user-facing
                "old_text": {"type": "string", "description": "要查找的文本"},  # i18n: user-facing
                "new_text": {"type": "string", "description": "替换后的文本"},  # i18n: user-facing
                "use_regex": {"type": "boolean", "default": False, "description": "使用正则匹配"},  # i18n: user-facing
                "create_backup": {"type": "boolean", "default": True, "description": "创建备份"},  # i18n: user-facing
                "dry_run": {"type": "boolean", "default": True, "description": "预览模式"},  # i18n: user-facing
            },
            "required": ["file_path", "old_text", "new_text"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        file_path = inputs.get("file_path", params.get("file_path", ""))
        old_text = inputs.get("old_text", params.get("old_text", ""))
        new_text = inputs.get("new_text", params.get("new_text", ""))
        use_regex = params.get("use_regex", False)
        create_backup = params.get("create_backup", True)
        dry_run = params.get("dry_run", True)

        if not file_path or not old_text:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="缺少文件路径或查找文本",  # i18n: user-facing
                summary="参数不完整",  # i18n: user-facing
            )

        path = Path(file_path).expanduser()
        # CR-19: file-write nodes must pass scoped_folder validation; reject out-of-bounds writes (backup and write share the same path)
        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if not scope.ensure_allowed(path):
            logger.warning(f"apply_edit sandbox rejected out-of-bounds path: {path}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"路径越界, 不在允许范围: {path}",  # i18n: user-facing
                summary="沙箱拒绝",  # i18n: user-facing
            )
        if not path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"文件不存在: {file_path}",  # i18n: user-facing
                summary="文件不存在",  # i18n: user-facing
            )

        try:
            content = path.read_text(encoding="utf-8")

            if use_regex:
                import re

                new_content, count = re.subn(old_text, new_text, content)
            else:
                count = content.count(old_text)
                new_content = content.replace(old_text, new_text)

            if count == 0:
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"success": True, "changes": 0, "file_path": str(path)},
                    summary="未找到匹配内容",  # i18n: user-facing
                )

            if dry_run:
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={
                        "success": True,
                        "changes": count,
                        "file_path": str(path),
                        "preview": new_content[:500] if len(new_content) > 500 else new_content,
                    },
                    summary=f"预览: 将修改 {count} 处",  # i18n: user-facing
                )

            # Create backup (E-4: atomic write, tmp+os.replace, prevents half-write corruption)
            backup_path = None
            if create_backup:
                backup_path = path.with_suffix(f"{path.suffix}.bak")
                _atomic_write_text(backup_path, content)

            # Write the modification (E-4: atomic write)
            _atomic_write_text(path, new_content)

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "success": True,
                    "changes": count,
                    "file_path": str(path),
                    "backup_path": str(backup_path) if create_backup else "",
                },
                summary=f"已修改 {count} 处",  # i18n: user-facing
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"编辑失败: {e}",  # i18n: user-facing
                summary="编辑失败",  # i18n: user-facing
            )


@register_node
class WorktreeNode(BaseNode):
    """Git worktree isolation node — P2-1.

    Executes commands in an isolated git worktree without polluting the main workspace.
    Supports the create/exec/remove actions.
    """

    name = "worktree"
    display_name = "Worktree 隔离"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "git worktree 隔离工作树: 创建/执行/删除/合并回基线"  # i18n: user-facing
    icon = "🌿"
    default_label = "Worktree"

    inputs = [
        {"key": "command", "label": "命令", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "result", "label": "结果", "type": "object"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "exec", "remove", "list", "merge_back"],
                    "description": "操作类型",  # i18n: user-facing
                    "default": "exec",
                },
                "name": {
                    "type": "string",
                    "description": "worktree 名称",  # i18n: user-facing
                },
                "branch": {
                    "type": "string",
                    "description": "checkout 已有分支 (空则新建 worktree/<name>)",  # i18n: user-facing
                    "default": "",
                },
                "command": {
                    "type": "string",
                    "description": "exec 动作: 在 worktree 内执行的命令",  # i18n: user-facing
                    "default": "",
                },
                "repo_root": {
                    "type": "string",
                    "description": "git 仓库根 (空则自动检测)",  # i18n: user-facing
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    "description": "exec 超时 (秒)",  # i18n: user-facing
                    "default": 30,
                },
                "source_repo": {
                    "type": "string",
                    "description": "create 动作: 来源仓库 URL; 与本仓 origin 不同则标记不可信, exec 被门禁阻止",  # i18n: user-facing
                    "default": "",
                },
                "trust_source": {
                    "type": "boolean",
                    "description": "显式信任该 worktree 的外部来源 (解除 exec 门禁)",  # i18n: user-facing
                    "default": False,
                },
                "target_branch": {
                    "type": "string",
                    "description": "merge_back 动作: 目标分支 (空则当前分支)",  # i18n: user-facing
                    "default": "",
                },
                "message": {
                    "type": "string",
                    "description": "merge_back 动作: 合并提交信息",  # i18n: user-facing
                    "default": "",
                },
            },
            "required": ["action", "name"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from ...utils.worktree import WorktreeManager

        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        action = params.get("action", "exec")
        name = inputs.get("name", params.get("name", ""))
        repo_root = params.get("repo_root", "")
        mgr = WorktreeManager(repo_root=repo_root)

        if action == "create":
            if not name:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            wt = mgr.create(
                name,
                branch=params.get("branch", ""),
                source_repo=params.get("source_repo", ""),
                trusted=bool(params.get("trust_source", False)),
            )
            if not wt:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="worktree 创建失败",  # i18n: user-facing
                    summary="创建失败",  # i18n: user-facing
                )  # i18n: user-facing
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=wt.to_dict(),
                summary=f"worktree 已创建: {name} ({wt.branch})",  # i18n: user-facing
            )
        elif action == "list":
            wts = mgr.list_worktrees()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"worktrees": [w.to_dict() for w in wts], "count": len(wts)},
                summary=f"共 {len(wts)} 个 worktree",  # i18n: user-facing
            )
        elif action == "remove":
            # issue #580: ownership-proof removal — no longer path-tail-segment matching; marker + porcelain cross-check
            if not name:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            ok = mgr.remove(name, force=True)
            return NodeResult(
                status=NodeStatus.SUCCESS if ok else NodeStatus.FAILED,
                data={"removed": ok, "name": name},
                summary=f"worktree 删除: {name} ({'成功' if ok else '失败'})",  # i18n: user-facing
            )
        elif action == "exec":
            if not name:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            if params.get("trust_source") and mgr.is_untrusted(name):
                if not mgr.mark_trusted(name):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error="信任标记失败",  # i18n: user-facing
                        summary="门禁错误",  # i18n: user-facing
                    )  # i18n: user-facing
            command = inputs.get("command", params.get("command", ""))
            if not command:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="缺少 command",  # i18n: user-facing
                    summary="参数缺失",  # i18n: user-facing
                )  # i18n: user-facing
            result = await mgr.exec_in(name, command, timeout=params.get("timeout", 30))
            if "error" in result:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error=result["error"], data=result, summary="执行失败")
            return NodeResult(
                status=NodeStatus.SUCCESS if result.get("return_code", 1) == 0 else NodeStatus.FAILED,
                data=result,
                summary=f"worktree exec rc={result.get('return_code')}",
            )
        elif action == "merge_back":
            # issue #580: merge runs in the baseline working tree, never occupying the agent worktree
            if not name:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            result = mgr.merge_back(
                name, target_branch=params.get("target_branch", ""), message=params.get("message", "")
            )
            if "error" in result:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.FAILED, error=result["error"], data=result, summary="合并失败")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=result,
                summary=f"已合并 {name} ({result.get('branch')}) -> {result.get('target')}",  # i18n: user-facing
            )
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"未知 action: {action}",  # i18n: user-facing
            summary="参数错误",  # i18n: user-facing
        )  # i18n: user-facing


@register_node
class LSPNode(BaseNode):
    """LSP code intelligence node — P2-3.

    Queries definition/references/hover/completion via the LSP client.
    """

    name = "lsp"
    display_name = "LSP 代码智能"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "LSP 查询: 定义/引用/悬停/补全"  # i18n: user-facing
    icon = "🔍"
    default_label = "LSP 查询"  # i18n: user-facing

    inputs = [
        {"key": "path", "label": "文件路径", "type": "string"},  # i18n: user-facing
        {"key": "line", "label": "行 (0-based)", "type": "integer"},  # i18n: user-facing
        {"key": "character", "label": "列 (0-based)", "type": "integer"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "result", "label": "结果", "type": "object"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definition", "references", "hover", "completion"],
                    "description": "查询类型",  # i18n: user-facing
                    "default": "hover",
                },
                "path": {
                    "type": "string",
                    "description": "目标文件绝对路径",  # i18n: user-facing
                },
                "line": {
                    "type": "integer",
                    "description": "行 (0-based)",  # i18n: user-facing
                    "default": 0,
                },
                "character": {
                    "type": "integer",
                    "description": "列 (0-based)",  # i18n: user-facing
                    "default": 0,
                },
                "root": {
                    "type": "string",
                    "description": "LSP workspace 根 (空则取文件所在目录)",  # i18n: user-facing
                    "default": "",
                },
            },
            "required": ["action", "path"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from ...code import query as lsp_query

        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        action = params.get("action", "hover")
        path = inputs.get("path", params.get("path", ""))
        if not path:
            return NodeResult(status=NodeStatus.FAILED, error="缺少 path", summary="参数缺失")  # i18n: user-facing
        line = int(inputs.get("line", params.get("line", 0)))
        character = int(inputs.get("character", params.get("character", 0)))
        root = params.get("root", "")
        result = await lsp_query(action, path, line, character, root=root)
        if "error" in result:
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=result["error"], data=result, summary="LSP 查询失败")
        return NodeResult(status=NodeStatus.SUCCESS, data=result, summary=f"LSP {action} OK")


@register_node
class PushNode(BaseNode):
    """Mobile push notification node — P2-5.

    Cross-platform mobile push (Bark/ntfy); degrades to a local macOS notification when unconfigured.
    """

    name = "push"
    display_name = "移动推送"  # i18n: user-facing
    category = NodeCategory.TOOL
    description = "移动推送通知 (Bark/ntfy/本地降级)"  # i18n: user-facing
    icon = "📱"
    default_label = "移动推送"  # i18n: user-facing

    inputs = [
        {"key": "title", "label": "标题", "type": "string"},  # i18n: user-facing
        {"key": "message", "label": "内容", "type": "string"},  # i18n: user-facing
    ]
    outputs = [
        {"key": "result", "label": "推送结果", "type": "object"},  # i18n: user-facing
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["auto", "bark", "ntfy", "local"],
                    "default": "auto",
                    "description": "推送渠道 (auto 按 url 自动判定)",  # i18n: user-facing
                },
                "url": {"type": "string", "default": "", "description": "Bark/ntfy server URL"},
                "token": {"type": "string", "default": "", "description": "Bark device key / ntfy topic"},
                "sound": {"type": "string", "default": "", "description": "提示音 (Bark)"},  # i18n: user-facing
                "priority": {"type": "string", "default": "", "description": "优先级 (ntfy: 1-5)"},  # i18n: user-facing
                "group": {"type": "string", "default": "", "description": "分组"},  # i18n: user-facing
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from ...notification import push as push_send

        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        title = inputs.get("title", "Fusion-Cowork")
        message = inputs.get("message", "")
        if not message:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="推送内容不能为空",  # i18n: user-facing
                summary="未指定内容",  # i18n: user-facing
            )  # i18n: user-facing

        result = await push_send(
            title,
            message,
            provider=params.get("provider", "auto"),
            url=params.get("url", ""),
            token=params.get("token", ""),
            sound=params.get("sound", ""),
            priority=params.get("priority", ""),
            group=params.get("group", ""),
        )
        data = {
            "success": result.success,
            "provider": result.provider,
            "degraded": result.degraded,
            "response": result.response,
            "error": result.error,
        }
        if result.success:
            tag = " (降级本地)" if result.degraded else ""  # i18n: user-facing
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data=data, summary=f"推送成功 via {result.provider}{tag}")
        return NodeResult(
            status=NodeStatus.FAILED,
            error=result.error,
            data=data,
            summary="推送失败",  # i18n: user-facing
        )  # i18n: user-facing
