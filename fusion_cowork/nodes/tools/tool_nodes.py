"""通用工具节点 — 吸纳自 Squish 内置工具集（13 个内置工具模式）。

所有工具节点通过 fusion-mlx 或本地能力执行，不依赖外部服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
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

logger = logging.getLogger(__name__)

# 命令沙箱 — 黑名单/白名单
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
            return f"命令被沙箱阻止: 含有禁止模式 '{blocked}'"
    for prefix in _SHELL_BLACKLIST_PREFIXES:
        if cmd_lower.startswith(prefix):
            return f"命令被沙箱阻止: 匹配禁止前缀 '{prefix}'"
    return None


# E-11: SSRF 防御 — 拒私有/环回/链路本地/云元数据地址
_SSRF_BANNED_HOSTS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",  # GCP 元数据
        "169.254.169.254",  # AWS/Azure 元数据
        "fd00.169.254.169.254",
    }
)


def _check_ssrf_url(url: str) -> Optional[str]:
    """解析 URL 主机, 解析所有 A/AAAA, 任一落在内网/环回/链路本地 → 拒。

    返回拒绝原因 str, 通过则 None。解析失败 (畸形 host) → 拒 (保守)。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return "URL 无主机名"
    host_lower = host.lower().rstrip(".")
    if host_lower in _SSRF_BANNED_HOSTS:
        return f"主机 '{host}' 在 SSRF 黑名单 (内网/元数据)"

    # 字面量 IP 直接判
    try:
        ip = ipaddress.ip_address(host_lower)
    except ValueError:
        ip = None
    if ip is not None and _is_private_ip(ip):
        return f"URL 主机 {ip} 属内网/环回/链路本地地址"

    # 域名: 解析所有地址, 任一内网 → 拒 (防 DNS rebinding + split-horizon)
    if ip is None:
        try:
            infos = socket.getaddrinfo(host_lower, None)
        except OSError as e:
            return f"主机 '{host}' DNS 解析失败: {e}"
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
                return f"主机 '{host}' 解析到内网地址 {resolved} (SSRF 拒绝)"
    return None


def _is_private_ip(ip) -> bool:
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _check_python_code(code: str) -> Optional[str]:
    # MD-18: AST 遍历替代正则黑名单 (正则可被注释/字符串/unicode 绕过)
    # parse 失败 (语法错误) 仍放行交子进程报错; 仅拒危险结构
    # A-4 补强: __import__/importlib/getattr(__builtins__)/eval/exec 等动态导入/执行绕过拦截
    import ast

    _DANGEROUS_IMPORTS = _PYTHON_BLACKLIST_IMPORTS
    _DANGEROUS_ATTRS = {
        ("os", "system"),
        ("os", "popen"),
    }
    _DANGEROUS_ATTR_SUBSTR = ("subprocess",)
    # A-4: 危险内置名 — __import__ 动态导入 / importlib 动态加载 / eval·exec 动态执行 /
    # getattr(__builtins__, ...) 取危险内置 / __builtins__ 直访
    _DANGEROUS_BUILTIN_CALLS = frozenset(
        {"__import__", "eval", "exec", "compile", "breakpoint", "globals", "locals", "vars"}
    )
    _DANGEROUS_NAMES = frozenset({"__import__", "importlib", "builtins", "__builtins__"})
    _DANGEROUS_ATTR_NAMES = frozenset({"__import__", "import_module", "exec", "eval", "compile"})
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 语法错误交子进程报错, 不在此拦截 (避免误伤)
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == m or alias.name.startswith(m + ".") for m in _DANGEROUS_IMPORTS):
                    return f"代码被沙箱阻止: 禁止导入 '{alias.name}'"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod == m or mod.startswith(m + ".") for m in _DANGEROUS_IMPORTS):
                return f"代码被沙箱阻止: 禁止 from '{mod}' 导入"
        elif isinstance(node, ast.Call):
            func = node.func
            # A-4: __import__("subprocess") — Name 直接调危险内置
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTIN_CALLS:
                return f"代码被沙箱阻止: 禁止调用内置 '{func.id}()' (可绕过静态导入拦截)"
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                attr_pair = (func.value.id, func.attr)
                if attr_pair in _DANGEROUS_ATTRS:
                    return f"代码被沙箱阻止: 禁止调用 {attr_pair[0]}.{attr_pair[1]}()"
                if func.value.id in _DANGEROUS_ATTR_SUBSTR:
                    return f"代码被沙箱阻止: 禁止使用 {func.value.id}.{func.attr}"
                # A-4: importlib.import_module(...) / builtins.__import__(...)
                if func.value.id in _DANGEROUS_NAMES or func.attr in _DANGEROUS_ATTR_NAMES:
                    return f"代码被沙箱阻止: 禁止动态导入/执行 {func.value.id}.{func.attr}()"
        # A-4: 裸引用 __import__ / importlib / __builtins__ 作值 (如赋给变量再调) 也拒
        if isinstance(node, ast.Name) and node.id in _DANGEROUS_NAMES:
            ctx_is_load = isinstance(node.ctx, ast.Load)
            if ctx_is_load:
                return f"代码被沙箱阻止: 禁止引用 '{node.id}' (动态导入/执行逃逸面)"
        # A-4: getattr(__builtins__, "eval") / getattr(builtins, "system") — 经 getattr 取危险对象
        if isinstance(node, ast.Call):
            f2 = node.func
            if isinstance(f2, ast.Name) and f2.id == "getattr" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in _DANGEROUS_NAMES:
                    return f"代码被沙箱阻止: 禁止 getattr({first.id}, ...) 取动态对象"
    return None


_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_repl_variables(variables: Dict[str, Any]) -> Optional[str]:
    # CR-13c: 注入变量名必须合法 Python 标识符, 拒特殊字符 (防脚本注入)
    for key in variables:
        if not isinstance(key, str) or not _IDENT_RE.match(key):
            return f"变量名非法 (非合法标识符): {key!r}"
        if key.startswith("__"):
            return f"变量名非法 (dunder 前缀): {key!r}"
    return None


@register_node
class ShellExecNode(BaseNode):
    """Shell 命令执行节点 — 执行本地 shell 命令并获取输出。

    吸纳自 Squish 的 squish_run_shell 工具。
    通过 subprocess 安全执行，支持超时和输出捕获。
    """

    name = "shell_exec"
    display_name = "Shell 命令执行"
    category = NodeCategory.TOOL
    description = "执行本地 shell 命令并获取输出"
    icon = "💻"
    default_label = "Shell 命令"

    inputs = [
        {"key": "command", "label": "命令", "type": "string"},
    ]
    outputs = [
        {"key": "stdout", "label": "标准输出", "type": "string"},
        {"key": "stderr", "label": "错误输出", "type": "string"},
        {"key": "return_code", "label": "返回码", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒）",
                    "default": 30,
                },
                "workdir": {
                    "type": "string",
                    "description": "工作目录",
                    "default": "",
                },
                "capture_output": {
                    "type": "boolean",
                    "description": "是否捕获输出",
                    "default": True,
                },
                "shell": {
                    "type": "boolean",
                    "description": "是否通过 shell 执行 (默认 False, 仅显式开启走 shell=True + WARNING)",
                    "default": False,
                },
            },
            "required": ["command"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        # 参数强制转换（吸纳自 Squish _coerce_* 机制）
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        command = inputs.get("command", params.get("command", ""))
        if not command:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="未指定命令",
                summary="未指定命令",
            )

        sandbox_error = _check_shell_command(command)
        if sandbox_error:
            logger.warning(f"ShellExec 沙箱拦截: {command[:60]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=sandbox_error,
                summary="沙箱拦截",
            )

        timeout = params.get("timeout", 30)
        workdir = params.get("workdir", "")
        capture = params.get("capture_output", True)
        use_shell = params.get("shell", False)

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if workdir and not scope.ensure_allowed(workdir):
            logger.warning(f"ShellExec workdir 越界被沙箱拒绝: {workdir}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"沙箱拒绝越界工作目录: {workdir}",
                summary="沙箱拦截 workdir",
            )

        try:
            if use_shell:
                logger.warning(f"ShellExec shell=True (纵深防御黑名单已过): {command[:80]}")
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.PIPE if capture else None,
                    cwd=workdir or None,
                )
            else:
                # CR-6/7: use_shell=False 走 exec 列表 (不起 /bin/sh), 消除 shell 注入
                argv = shlex.split(command)
                if not argv:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error="命令解析为空",
                        summary="空命令",
                    )
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.PIPE if capture else None,
                    cwd=workdir or None,
                )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"命令执行超时 ({timeout}s)",
                    data={"command": command, "timeout": timeout},
                    summary="执行超时",
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
                summary=f"命令 {'成功' if return_code == 0 else '失败'} (返回码: {return_code})",
            )

        except FileNotFoundError as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"命令未找到: {e}",
                summary="命令未找到",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"执行异常: {e}",
                summary="执行异常",
            )


@register_node
class PythonREPLNode(BaseNode):
    """Python REPL 执行节点 — 执行 Python 代码片段。

    吸纳自 Squish 的 squish_python_repl 工具。
    在隔离的子进程中执行，限制资源使用。
    """

    name = "python_repl"
    display_name = "Python REPL"
    category = NodeCategory.TOOL
    description = "执行 Python 代码片段并返回结果"
    icon = "🐍"
    default_label = "Python 执行"

    inputs = [
        {"key": "code", "label": "Python 代码", "type": "string"},
    ]
    outputs = [
        {"key": "result", "label": "执行结果", "type": "string"},
        {"key": "error", "label": "错误信息", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒）",
                    "default": 15,
                },
                "variables": {
                    "type": "object",
                    "description": "注入的变量（JSON 对象）",
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
                error="未指定 Python 代码",
                summary="未指定代码",
            )

        sandbox_error = _check_python_code(code)
        if sandbox_error:
            logger.warning(f"PythonREPL 沙箱拦截: {code[:60]}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=sandbox_error,
                summary="沙箱拦截",
            )

        timeout = params.get("timeout", 15)
        variables = params.get("variables", {})

        # CR-13c: 变量名合法性校验, 拒脚本注入
        var_error = _validate_repl_variables(variables)
        if var_error:
            logger.warning(f"PythonREPL 变量名校验拦截: {var_error}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=var_error,
                summary="沙箱拦截",
            )

        # 构建执行脚本
        script_lines = [
            "import json, sys, math, os, datetime, collections, itertools, random, re, pathlib",
            "",
        ]
        # 注入变量
        for key, value in variables.items():
            script_lines.append(f"{key} = {json.dumps(value)}")
        if variables:
            script_lines.append("")

        script_lines.extend(
            [
                "try:",
                "    _result = eval(sys.stdin.read())",
                "    print(json.dumps({'result': repr(_result), 'type': type(_result).__name__}))",
                "except SyntaxError:",
                "    exec(sys.stdin.read())",
                "    print(json.dumps({'result': 'executed', 'type': 'NoneType'}))",
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
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=code.encode("utf-8")),
                    timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"Python 执行超时 ({timeout}s)",
                    summary="执行超时",
                )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            # 解析结果
            try:
                result_data = json.loads(stdout_str) if stdout_str else {}
            except json.JSONDecodeError:
                result_data = {"result": stdout_str, "type": "str"}

            if "error" in result_data:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=result_data["error"],
                    data={"result": "", "error": result_data["error"], "stderr": stderr_str},
                    summary="执行错误",
                )

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "result": result_data.get("result", stdout_str),
                    "type": result_data.get("type", "str"),
                    "stderr": stderr_str,
                },
                summary=f"执行成功: {result_data.get('result', '')[:100]}",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Python REPL 异常: {e}",
                summary="执行异常",
            )


@register_node
class WebSearchNode(BaseNode):
    """Web 搜索节点 — 搜索网页内容。

    吸纳自 Squish 的 squish_web_search 工具。
    通过 DuckDuckGo Lite 接口实现，无需 API Key。
    """

    name = "web_search"
    display_name = "Web 搜索"
    category = NodeCategory.TOOL
    description = "搜索网页内容（通过 DuckDuckGo）"
    icon = "🌐"
    default_label = "Web 搜索"

    inputs = [
        {"key": "query", "label": "搜索关键词", "type": "string"},
    ]
    outputs = [
        {"key": "results", "label": "搜索结果", "type": "list[dict]"},
        {"key": "result_count", "label": "结果数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "default": 5, "description": "最大结果数"},
                "timeout": {"type": "integer", "default": 10, "description": "超时秒数"},
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
                error="未指定搜索关键词",
                summary="未指定搜索词",
            )

        max_results = params.get("max_results", 5)
        timeout = params.get("timeout", 10)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout) as client:
                # 使用 DuckDuckGo Lite API
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                )
                resp.raise_for_status()

                import re

                html = resp.text

                # 解析结果
                results = []
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

                import html as html_mod

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

            return NodeResult(
                status=NodeStatus.SUCCESS if results else NodeStatus.FAILED,
                data={
                    "results": results,
                    "result_count": len(results),
                    "query": query,
                },
                summary=f"找到 {len(results)} 条结果",
            )

        except ImportError:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="需要 httpx 库: pip install httpx",
                summary="依赖缺失",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"搜索失败: {e}",
                summary="搜索失败",
            )


@register_node
class FetchURLNode(BaseNode):
    """网页获取节点 — 获取 URL 内容并返回文本。

    吸纳自 Squish 的 squish_fetch_url 工具。
    支持 HTTP/HTTPS，自动转换 HTML 为文本。
    """

    name = "fetch_url"
    display_name = "获取网页"
    category = NodeCategory.TOOL
    description = "获取 URL 内容并返回文本"
    icon = "📄"
    default_label = "获取网页"

    inputs = [
        {"key": "url", "label": "URL", "type": "string"},
    ]
    outputs = [
        {"key": "content", "label": "内容", "type": "string"},
        {"key": "status_code", "label": "状态码", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要获取的 URL"},
                "timeout": {"type": "integer", "default": 15, "description": "超时秒数"},
                "max_chars": {"type": "integer", "default": 100000, "description": "最大字符数"},
                "extract_text": {"type": "boolean", "default": True, "description": "提取纯文本"},
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
                error="未指定 URL",
                summary="未指定 URL",
            )

        # 安全检查（吸纳自 Squish 的 URL 验证）
        if not url.startswith(("http://", "https://")):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"不安全的 URL 协议: {url.split('://')[0] if '://' in url else 'unknown'}://",
                summary="URL 协议不安全",
            )

        # E-11: SSRF 防御 — 解析主机, 拒私有/环回/链路本地/元数据地址
        ssrf_err = _check_ssrf_url(url)
        if ssrf_err:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=ssrf_err,
                summary="URL 指向内网/元数据地址, 已拒 (SSRF 防御)",
            )

        timeout = params.get("timeout", 15)
        max_chars = params.get("max_chars", 100000)
        extract_text = params.get("extract_text", True)

        try:
            import httpx

            # E-11: follow_redirects=False — 防重定向绕过主机校验 (重定向目标未经 SSRF 检查)
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

                # 根据内容类型处理
                if "text/html" in content_type and extract_text:
                    import re

                    html = resp.text
                    # 简单的 HTML 转文本
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
                summary=f"获取成功 ({len(content)} 字符)",
            )

        except ImportError:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="需要 httpx 库: pip install httpx",
                summary="依赖缺失",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"获取失败: {e}",
                summary="获取失败",
            )


@register_node
class ApplyEditNode(BaseNode):
    """文件编辑节点 — 对文件应用编辑操作（查找替换）。

    吸纳自 Squish 的 squish_apply_edit 工具。
    支持精确查找替换和正则替换。
    """

    name = "apply_edit"
    display_name = "文件编辑"
    category = NodeCategory.TOOL
    description = "对文件应用编辑操作（查找替换）"
    icon = "✏️"
    default_label = "文件编辑"

    inputs = [
        {"key": "file_path", "label": "文件路径", "type": "string"},
        {"key": "old_text", "label": "查找文本", "type": "string"},
        {"key": "new_text", "label": "替换文本", "type": "string"},
    ]
    outputs = [
        {"key": "success", "label": "是否成功", "type": "boolean"},
        {"key": "changes", "label": "变更数", "type": "integer"},
        {"key": "file_path", "label": "文件路径", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要编辑的文件路径"},
                "old_text": {"type": "string", "description": "要查找的文本"},
                "new_text": {"type": "string", "description": "替换后的文本"},
                "use_regex": {"type": "boolean", "default": False, "description": "使用正则匹配"},
                "create_backup": {"type": "boolean", "default": True, "description": "创建备份"},
                "dry_run": {"type": "boolean", "default": True, "description": "预览模式"},
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
                error="缺少文件路径或查找文本",
                summary="参数不完整",
            )

        path = Path(file_path).expanduser()
        # CR-19: 文件写节点须经 scoped_folder 校验, 拒越界写入 (备份与写入同路径)
        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if not scope.ensure_allowed(path):
            logger.warning(f"apply_edit 沙箱拒绝越界路径: {path}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"路径越界, 不在允许范围: {path}",
                summary="沙箱拒绝",
            )
        if not path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"文件不存在: {file_path}",
                summary="文件不存在",
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
                    summary="未找到匹配内容",
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
                    summary=f"预览: 将修改 {count} 处",
                )

            # 创建备份
            if create_backup:
                backup_path = path.with_suffix(f"{path.suffix}.bak")
                backup_path.write_text(content, encoding="utf-8")

            # 写入修改
            path.write_text(new_content, encoding="utf-8")

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "success": True,
                    "changes": count,
                    "file_path": str(path),
                    "backup_path": str(backup_path) if create_backup else "",
                },
                summary=f"已修改 {count} 处",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"编辑失败: {e}",
                summary="编辑失败",
            )


@register_node
class WorktreeNode(BaseNode):
    """Git Worktree 隔离节点 — P2-1。

    在独立 git worktree 中执行命令, 不污染主工作区。
    支持 create/exec/remove 三种 action。
    """

    name = "worktree"
    display_name = "Worktree 隔离"
    category = NodeCategory.TOOL
    description = "git worktree 隔离工作树: 创建/执行/删除"
    icon = "🌿"
    default_label = "Worktree"

    inputs = [
        {"key": "command", "label": "命令", "type": "string"},
    ]
    outputs = [
        {"key": "result", "label": "结果", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "exec", "remove", "list"],
                    "description": "操作类型",
                    "default": "exec",
                },
                "name": {
                    "type": "string",
                    "description": "worktree 名称",
                },
                "branch": {
                    "type": "string",
                    "description": "checkout 已有分支 (空则新建 worktree/<name>)",
                    "default": "",
                },
                "command": {
                    "type": "string",
                    "description": "exec 动作: 在 worktree 内执行的命令",
                    "default": "",
                },
                "repo_root": {
                    "type": "string",
                    "description": "git 仓库根 (空则自动检测)",
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    "description": "exec 超时 (秒)",
                    "default": 30,
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
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            wt = mgr.create(name, branch=params.get("branch", ""))
            if not wt:
                return NodeResult(status=NodeStatus.FAILED, error="worktree 创建失败", summary="创建失败")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=wt.to_dict(),
                summary=f"worktree 已创建: {name} ({wt.branch})",
            )
        elif action == "list":
            wts = mgr.list_worktrees()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"worktrees": [w.to_dict() for w in wts], "count": len(wts)},
                summary=f"共 {len(wts)} 个 worktree",
            )
        elif action == "remove":
            if not name:
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            if not mgr.get(name):
                for w in mgr.list_worktrees():
                    if Path(w.path).name == name:
                        mgr._worktrees[name] = w
                        break
            ok = mgr.remove(name, force=True)
            return NodeResult(
                status=NodeStatus.SUCCESS if ok else NodeStatus.FAILED,
                data={"removed": ok, "name": name},
                summary=f"worktree 删除: {name} ({'成功' if ok else '失败'})",
            )
        elif action == "exec":
            if not name:
                return NodeResult(status=NodeStatus.FAILED, error="缺少 worktree name", summary="参数缺失")
            # 若未登记则尝试登记现有 worktree (按名称匹配路径末段)
            if not mgr.get(name):
                for w in mgr.list_worktrees():
                    if Path(w.path).name == name:
                        mgr._worktrees[name] = w
                        break
            command = inputs.get("command", params.get("command", ""))
            if not command:
                return NodeResult(status=NodeStatus.FAILED, error="缺少 command", summary="参数缺失")
            result = await mgr.exec_in(name, command, timeout=params.get("timeout", 30))
            if "error" in result:
                return NodeResult(status=NodeStatus.FAILED, error=result["error"], data=result, summary="执行失败")
            return NodeResult(
                status=NodeStatus.SUCCESS if result.get("return_code", 1) == 0 else NodeStatus.FAILED,
                data=result,
                summary=f"worktree exec rc={result.get('return_code')}",
            )
        return NodeResult(status=NodeStatus.FAILED, error=f"未知 action: {action}", summary="参数错误")


@register_node
class LSPNode(BaseNode):
    """LSP 代码智能节点 — P2-3。

    经 LSP 客户端查询 definition/references/hover/completion。
    """

    name = "lsp"
    display_name = "LSP 代码智能"
    category = NodeCategory.TOOL
    description = "LSP 查询: 定义/引用/悬停/补全"
    icon = "🔍"
    default_label = "LSP 查询"

    inputs = [
        {"key": "path", "label": "文件路径", "type": "string"},
        {"key": "line", "label": "行 (0-based)", "type": "integer"},
        {"key": "character", "label": "列 (0-based)", "type": "integer"},
    ]
    outputs = [
        {"key": "result", "label": "结果", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definition", "references", "hover", "completion"],
                    "description": "查询类型",
                    "default": "hover",
                },
                "path": {
                    "type": "string",
                    "description": "目标文件绝对路径",
                },
                "line": {
                    "type": "integer",
                    "description": "行 (0-based)",
                    "default": 0,
                },
                "character": {
                    "type": "integer",
                    "description": "列 (0-based)",
                    "default": 0,
                },
                "root": {
                    "type": "string",
                    "description": "LSP workspace 根 (空则取文件所在目录)",
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少 path", summary="参数缺失")
        line = int(inputs.get("line", params.get("line", 0)))
        character = int(inputs.get("character", params.get("character", 0)))
        root = params.get("root", "")
        result = await lsp_query(action, path, line, character, root=root)
        if "error" in result:
            return NodeResult(status=NodeStatus.FAILED, error=result["error"], data=result, summary="LSP 查询失败")
        return NodeResult(status=NodeStatus.SUCCESS, data=result, summary=f"LSP {action} OK")


@register_node
class PushNode(BaseNode):
    """移动推送通知节点 — P2-5。

    跨平台移动推送 (Bark/ntfy), 未配置时降级本地 macOS 通知。
    """

    name = "push"
    display_name = "移动推送"
    category = NodeCategory.TOOL
    description = "移动推送通知 (Bark/ntfy/本地降级)"
    icon = "📱"
    default_label = "移动推送"

    inputs = [
        {"key": "title", "label": "标题", "type": "string"},
        {"key": "message", "label": "内容", "type": "string"},
    ]
    outputs = [
        {"key": "result", "label": "推送结果", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["auto", "bark", "ntfy", "local"],
                    "default": "auto",
                    "description": "推送渠道 (auto 按 url 自动判定)",
                },
                "url": {"type": "string", "default": "", "description": "Bark/ntfy server URL"},
                "token": {"type": "string", "default": "", "description": "Bark device key / ntfy topic"},
                "sound": {"type": "string", "default": "", "description": "提示音 (Bark)"},
                "priority": {"type": "string", "default": "", "description": "优先级 (ntfy: 1-5)"},
                "group": {"type": "string", "default": "", "description": "分组"},
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
            return NodeResult(status=NodeStatus.FAILED, error="推送内容不能为空", summary="未指定内容")

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
            tag = " (降级本地)" if result.degraded else ""
            return NodeResult(status=NodeStatus.SUCCESS, data=data, summary=f"推送成功 via {result.provider}{tag}")
        return NodeResult(status=NodeStatus.FAILED, error=result.error, data=data, summary="推送失败")
