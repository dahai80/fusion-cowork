"""Fusion-Cowork 内嵌浏览器集成 — macOS WKWebView 原生浏览器模块。

通过 CLI 控制浏览器：启动、打开 URL、自动化、状态查询。
与 Swift 原生浏览器通过 HTTP 桥接通信。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ...engine.node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus,
    NodeCategory, register_node, coerce_params,
)

logger = logging.getLogger(__name__)

# 浏览器桥接服务端口
BROWSER_BRIDGE_PORT = 9234
# 浏览器应用路径
BROWSER_APP_PATH = Path(__file__).parent.parent.parent.parent / "browser" / ".build" / "release" / "FusionBrowser.app"


class BrowserClient:
    """浏览器桥接客户端 — 与 Swift 原生浏览器通信。"""

    def __init__(self, port: int = BROWSER_BRIDGE_PORT):
        self.port = port
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"http://localhost:{self.port}",
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def open_url(self, url: str) -> Dict[str, Any]:
        """在浏览器中打开 URL。"""
        resp = await self.client.post("/api/browser/open", json={"url": url})
        resp.raise_for_status()
        return resp.json()

    async def execute_script(self, script: str) -> Dict[str, Any]:
        """在激活标签页中执行 JS。"""
        resp = await self.client.post("/api/browser/execute", json={"script": script})
        resp.raise_for_status()
        return resp.json()

    async def get_page_text(self) -> str:
        """获取当前页面文本内容。"""
        resp = await self.client.get("/api/browser/page/text")
        resp.raise_for_status()
        data = resp.json()
        return data.get("text", "")

    async def get_page_html(self) -> str:
        """获取当前页面 HTML。"""
        resp = await self.client.get("/api/browser/page/html")
        resp.raise_for_status()
        data = resp.json()
        return data.get("html", "")

    async def click_element(self, selector: str) -> bool:
        """点击元素。"""
        resp = await self.client.post("/api/browser/click", json={"selector": selector})
        resp.raise_for_status()
        data = resp.json()
        return data.get("success", False)

    async def fill_input(self, selector: str, value: str) -> bool:
        """输入文本。"""
        resp = await self.client.post("/api/browser/fill", json={"selector": selector, "value": value})
        resp.raise_for_status()
        data = resp.json()
        return data.get("success", False)

    async def screenshot(self) -> Optional[bytes]:
        """截图。"""
        resp = await self.client.get("/api/browser/screenshot")
        resp.raise_for_status()
        return resp.content

    async def list_tabs(self) -> List[Dict[str, Any]]:
        """列出所有标签页。"""
        resp = await self.client.get("/api/browser/tabs")
        resp.raise_for_status()
        data = resp.json()
        return data.get("tabs", [])

    async def get_status(self) -> Dict[str, Any]:
        """获取浏览器状态。"""
        try:
            resp = await self.client.get("/api/browser/status", timeout=2.0)
            return resp.json()
        except Exception:
            return {"running": False}

    async def close_browser(self) -> bool:
        """关闭浏览器。"""
        try:
            resp = await self.client.post("/api/browser/close")
            return resp.status_code == 200
        except Exception:
            return False

    def is_running(self) -> bool:
        """检查浏览器是否在运行（通过进程检测）。"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "FusionBrowser"],
                capture_output=True, text=True, timeout=2,
            )
            return result.returncode == 0
        except Exception:
            return False


class BrowserManager:
    """浏览器进程管理器。"""

    @staticmethod
    def launch() -> bool:
        """启动浏览器应用。"""
        app_path = BROWSER_APP_PATH
        if not app_path.exists():
            logger.error(f"浏览器应用未找到: {app_path}")
            logger.info("请先构建: cd browser && swift build -c release")
            return False

        try:
            subprocess.Popen(
                ["open", str(app_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"已启动浏览器: {app_path}")
            return True
        except Exception as e:
            logger.error(f"启动浏览器失败: {e}")
            return False

    @staticmethod
    def build() -> bool:
        """构建浏览器应用。"""
        browser_dir = BROWSER_APP_PATH.parent.parent.parent
        try:
            result = subprocess.run(
                ["swift", "build", "-c", "release"],
                cwd=str(browser_dir),
                capture_output=True, text=True,
                timeout=300,
            )
            if result.returncode == 0:
                logger.info("浏览器构建成功")
                return True
            else:
                logger.error(f"构建失败:\n{result.stderr}")
                return False
        except Exception as e:
            logger.error(f"构建异常: {e}")
            return False


@register_node
class BrowserOpenNode(BaseNode):
    """浏览器打开节点 — 在浏览器中打开指定 URL。"""
    name = "browser_open"
    display_name = "浏览器打开"
    category = NodeCategory.TOOL
    description = "在 Fusion 内嵌浏览器中打开 URL"
    icon = "🌐"
    default_label = "浏览器打开"

    inputs = [
        {"key": "url", "label": "URL", "type": "string"},
    ]
    outputs = [
        {"key": "result", "label": "结果", "type": "dict"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要打开的 URL"},
                "new_tab": {"type": "boolean", "default": True, "description": "在新标签页打开"},
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

        client = BrowserClient()
        try:
            result = await client.open_url(url)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"result": result, "url": url},
                summary=f"已打开: {url}",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"打开失败: {e}",
                summary="打开失败",
            )
        finally:
            await client.close()


@register_node
class BrowserExtractNode(BaseNode):
    """浏览器内容提取节点 — 从网页提取文本/HTML。"""
    name = "browser_extract"
    display_name = "浏览器提取"
    category = NodeCategory.TOOL
    description = "从网页提取文本内容"
    icon = "📄"
    default_label = "提取内容"

    inputs = [
        {"key": "url", "label": "URL", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "text", "label": "文本内容", "type": "string"},
        {"key": "html", "label": "HTML 源码", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要提取的 URL（留空使用当前页面）"},
                "extract_html": {"type": "boolean", "default": False, "description": "是否提取 HTML"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        url = inputs.get("url", params.get("url", ""))
        extract_html = params.get("extract_html", False)

        client = BrowserClient()
        try:
            if url:
                await client.open_url(url)
                await asyncio.sleep(1)  # 等待页面加载

            text = await client.get_page_text()
            html = await client.get_page_html() if extract_html else ""

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "text": text,
                    "html": html,
                    "text_length": len(text),
                },
                summary=f"提取 {len(text)} 字符",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"提取失败: {e}",
                summary="提取失败",
            )
        finally:
            await client.close()


@register_node
class BrowserAutomateNode(BaseNode):
    """浏览器自动化节点 — 自动点击、输入、截图。"""
    name = "browser_automate"
    display_name = "浏览器自动化"
    category = NodeCategory.TOOL
    description = "自动操作网页元素"
    icon = "🤖"
    default_label = "网页自动化"

    inputs = [
        {"key": "url", "label": "URL", "type": "string", "optional": True},
        {"key": "actions", "label": "操作列表", "type": "list[dict]"},
    ]
    outputs = [
        {"key": "results", "label": "操作结果", "type": "list[dict]"},
        {"key": "screenshot", "label": "截图", "type": "bytes", "optional": True},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "actions": {
                    "type": "array",
                    "description": "操作列表，每项: {type: 'click'|'fill'|'wait'|'extract', selector: '...', value: '...'}",
                },
                "take_screenshot": {"type": "boolean", "default": False},
                "wait_between_actions": {"type": "number", "default": 0.5},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        url = inputs.get("url", params.get("url", ""))
        actions = inputs.get("actions", params.get("actions", []))
        take_screenshot = params.get("take_screenshot", False)
        wait_time = params.get("wait_between_actions", 0.5)

        client = BrowserClient()
        results = []

        try:
            # 打开页面
            if url:
                await client.open_url(url)
                await asyncio.sleep(1)

            # 执行操作
            for action in actions:
                action_type = action.get("type", "")
                selector = action.get("selector", "")
                value = action.get("value", "")

                if action_type == "click":
                    success = await client.click_element(selector)
                    results.append({"type": "click", "selector": selector, "success": success})
                elif action_type == "fill":
                    success = await client.fill_input(selector, value)
                    results.append({"type": "fill", "selector": selector, "value": value, "success": success})
                elif action_type == "extract":
                    text = await client.get_page_text()
                    results.append({"type": "extract", "text": text[:500], "length": len(text)})
                elif action_type == "wait":
                    await asyncio.sleep(float(value or wait_time))
                    results.append({"type": "wait", "duration": value or wait_time})

                await asyncio.sleep(wait_time)

            # 截图
            screenshot_data = None
            if take_screenshot:
                screenshot_data = await client.screenshot()

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "results": results,
                    "screenshot": screenshot_data,
                    "action_count": len(results),
                },
                summary=f"完成 {len(results)} 个操作",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"自动化失败: {e}",
                summary="自动化失败",
            )
        finally:
            await client.close()