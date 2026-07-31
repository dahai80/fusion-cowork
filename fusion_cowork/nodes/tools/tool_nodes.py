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
    BaseNode, NodeResult, NodeStatus,
    NodeCategory, register_node, coerce_params,
)

logger = logging.getLogger(__name__)

# 命令沙箱 — 黑名单/白名单
_SHELL_BLACKLIST = frozenset({
    "rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:",
    "shutdown", "reboot", "halt", "poweroff",
    "launchctl unload", "csrutil disable",
})

_SHELL_BLACKLIST_PREFIXES = frozenset({
    "rm -rf /", "rm -r /", "rm -f /",
})

_PYTHON_BLACKLIST_IMPORTS = frozenset({
    "ctypes", "multiprocessing", "subprocess",
    "socketserver", "http.server", "xmlrpc",
    "asyncio.subprocess", "os.system", "os.popen",
})


def _check_shell_command(command: str) -> Optional[str]:
    cmd_lower = command.strip().lower()
    for blocked in _SHELL_BLACKLIST:
        if blocked in cmd_lower:
            return f"命令被沙箱阻止: 含有禁止模式 '{blocked}'"
    for prefix in _SHELL_BLACKLIST_PREFIXES:
        if cmd_lower.startswith(prefix):
            return f"命令被沙箱阻止: 匹配禁止前缀 '{prefix}'"
    return None


def _check_python_code(code: str) -> Optional[str]:
    import re
    for mod in _PYTHON_BLACKLIST_IMPORTS:
        pattern = rf"\bimport\s+{re.escape(mod)}\b|\bfrom\s+{re.escape(mod)}\b"
        if re.search(pattern, code):
            return f"代码被沙箱阻止: 禁止导入 '{mod}'"
    if re.search(r"\bos\.system\s*\(", code):
        return "代码被沙箱阻止: 禁止调用 os.system()"
    if re.search(r"\bos\.popen\s*\(", code):
        return "代码被沙箱阻止: 禁止调用 os.popen()"
    if re.search(r"\bsubprocess\.", code):
        return "代码被沙箱阻止: 禁止使用 subprocess"
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
                    "description": "是否通过 shell 执行",
                    "default": True,
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
        use_shell = params.get("shell", True)

        try:
            proc = await asyncio.create_subprocess_shell(
                command if use_shell else shlex.split(command),
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                cwd=workdir or None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
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

        script_lines.extend([
            "try:",
            "    _result = eval(sys.stdin.read())",
            "    print(json.dumps({'result': repr(_result), 'type': type(_result).__name__}))",
            "except SyntaxError:",
            "    exec(sys.stdin.read())",
            "    print(json.dumps({'result': 'executed', 'type': 'NoneType'}))",
            "except Exception as e:",
            f"    print(json.dumps({{'error': str(e), 'type': type(e).__name__}}))",
        ])

        script = "\n".join(script_lines)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=code.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
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
                        snippet = html_mod.unescape(
                            re.sub(r'<[^>]+>', '', snippets[i]).strip()
                        )
                    results.append({
                        "title": html_mod.unescape(re.sub(r'<[^>]+>', '', title).strip()),
                        "url": url,
                        "snippet": snippet,
                    })

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

        timeout = params.get("timeout", 15)
        max_chars = params.get("max_chars", 100000)
        extract_text = params.get("extract_text", True)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
                })
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")

                # 根据内容类型处理
                if "text/html" in content_type and extract_text:
                    import re
                    html = resp.text
                    # 简单的 HTML 转文本
                    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = re.sub(r'\s+', ' ', text).strip()
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