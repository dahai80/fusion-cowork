"""Fusion-Cowork 内嵌浏览器集成 — macOS WKWebView 原生浏览器模块。

通过 CLI 控制浏览器：启动、打开 URL、自动化、状态查询。
与 Swift 原生浏览器通过 HTTP 桥接通信。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

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
                capture_output=True,
                text=True,
                timeout=2,
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
                capture_output=True,
                text=True,
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


@register_node
class ManagedBrowserNode(BaseNode):
    """托管浏览器节点 — 自动拉起独立 profile 的 Chromium 并开 CDP (issue #153)。

    action=launch: 启动托管浏览器 (独立 user-data-dir + 自动调试端口), 并把
    FUSION_BROWSER_CDP 指向该端口, 后续 cdp_* 节点直接复用同一会话。
    action=close / status: 关闭或查询当前托管浏览器。
    import_cookies: Netscape cookies.txt 路径, 启动后经 CDP Network.setCookies 导入。
    """

    name = "managed_browser"
    display_name = "托管浏览器"
    category = NodeCategory.TOOL
    description = "启动独立 profile 的 Chromium (自动 CDP 端口), 复用 cdp_* 节点"
    icon = "🌐"
    default_label = "托管浏览器"

    inputs = [{"key": "url", "label": "URL", "type": "string", "optional": True}]
    outputs = [
        {"key": "port", "label": "CDP 端口", "type": "integer"},
        {"key": "status", "label": "状态", "type": "dict"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["launch", "close", "status"], "default": "launch"},
                "url": {"type": "string", "default": "about:blank"},
                "port": {"type": "integer", "default": 0, "description": "0 = 自动分配"},
                "headless": {"type": "boolean", "default": False},
                "profile_dir": {"type": "string", "default": ""},
                "import_cookies": {"type": "string", "default": "", "description": "Netscape cookies.txt 路径"},
            },
            "required": ["action"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from ...managed_browser import ManagedBrowserError, get_managed_browser

        params = coerce_params(self.config.params, self.get_params_schema())
        action = str(params.get("action", "launch"))
        browser = get_managed_browser()
        try:
            if action == "close":
                closed = await browser.close()
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"closed": closed, "status": browser.status()},
                    summary="托管浏览器已关闭" if closed else "托管浏览器未运行",
                )
            if action == "status":
                status = browser.status()
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=status,
                    summary=f"托管浏览器端口 {status['port']}" if status.get("running") else "托管浏览器未运行",
                )
            url = str(inputs.get("url") or params.get("url") or "about:blank")
            info = await browser.launch(
                url=url,
                port=int(params.get("port") or 0),
                headless=bool(params.get("headless")),
                import_cookies=str(params.get("import_cookies") or ""),
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=info,
                summary=f"托管浏览器已启动 (CDP 端口 {info['port']})",
            )
        except ManagedBrowserError as e:
            logger.error(f"托管浏览器失败: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="托管浏览器启动失败")
        except Exception as e:
            logger.error(f"托管浏览器异常: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"托管浏览器异常: {e}", summary="托管浏览器失败")


@register_node
class FusionBrowserNode(BaseNode):
    name = "fusion_browser"
    display_name = "fusion-browser 引擎"
    category = NodeCategory.TOOL
    description = "托管 fusion-browser 引擎 (UDS): 每运行独立 profile + 登录态导入 + 面板截图流"
    icon = "🧭"
    default_label = "fusion-browser"

    inputs = [{"key": "url", "label": "URL", "type": "string", "optional": True}]
    outputs = [
        {"key": "session_id", "label": "会话", "type": "string"},
        {"key": "status", "label": "状态", "type": "dict"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "launch",
                        "create_session",
                        "import_cookies",
                        "execute",
                        "screenshot",
                        "close_session",
                        "delete_profile",
                        "finish",
                        "status",
                        "close",
                    ],
                    "default": "launch",
                },
                "run_id": {"type": "string", "default": "", "description": "工作流运行 ID (profile 归属)"},
                "profile_id": {"type": "string", "default": ""},
                "url": {"type": "string", "default": ""},
                "mode": {"type": "string", "enum": ["headless", "headed"], "default": "headless"},
                "session_id": {"type": "string", "default": ""},
                "browser": {"type": "string", "enum": ["firefox", "chrome", "edge"], "default": "firefox"},
                "browser_profile": {"type": "string", "default": ""},
                "cdp_action": {
                    "type": "string",
                    "enum": ["navigate", "click", "type_text", "scroll", "screenshot", "evaluate", "state"],
                    "default": "navigate",
                },
                "target_node_id": {"type": "string", "default": ""},
                "payload_text": {"type": "string", "default": ""},
                "allowed_origins": {"type": "array", "items": {"type": "string"}, "default": []},
                "keep_profile": {"type": "boolean", "default": False},
                "state_root": {"type": "string", "default": ""},
            },
            "required": ["action"],
        }

    def _browser(self, params: Dict[str, Any]):
        from ...fusion_browser import FusionBrowserManaged

        origins = [str(o) for o in (params.get("allowed_origins") or []) if str(o).strip()]
        state_root = params.get("state_root") or None
        if state_root or origins or params.get("mode"):
            return FusionBrowserManaged(
                allowed_origins=origins,
                state_root=state_root,
                mode=str(params.get("mode") or "headless"),
            )
        from ...fusion_browser import get_fusion_browser

        return get_fusion_browser()

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        from ...fusion_browser import FusionBrowserError

        params = coerce_params(self.config.params, self.get_params_schema())
        action = str(params.get("action", "launch"))
        browser = self._browser(params)
        url = str(inputs.get("url") or params.get("url") or "")
        session_id = str(params.get("session_id") or "")
        run_id = str(params.get("run_id") or "") or None
        try:
            if action == "launch":
                info = await asyncio.to_thread(browser.launch, url or None)
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=info,
                    summary=f"fusion-browser 已启动 pid={info.get('pid')}",
                )
            if action == "status":
                info = await asyncio.to_thread(browser.status)
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=info,
                    summary="fusion-browser 运行中" if info.get("running") else "fusion-browser 未运行",
                )
            if action == "create_session":
                profile_id = str(params.get("profile_id") or "") or None
                info = await asyncio.to_thread(
                    browser.create_session,
                    run_id=run_id,
                    profile_id=profile_id,
                    initial_url=url or None,
                    mode=str(params.get("mode") or "headless"),
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=info,
                    summary=f"会话已创建 {info['session_id']} (profile {info.get('profile_id') or '临时'})",
                )
            if action == "import_cookies":
                if not session_id:
                    return NodeResult(status=NodeStatus.FAILED, error="session_id 必填", summary="导入失败")
                counters = await asyncio.to_thread(
                    browser.import_cookies,
                    session_id,
                    str(params.get("browser") or "firefox"),
                    str(params.get("browser_profile") or "") or None,
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=counters,
                    summary=(
                        f"登录态导入 {counters['browser']}: imported {counters['imported']}, "
                        f"skipped_app_bound {counters['skipped_app_bound']}, "
                        f"skipped_other {counters['skipped_other']}"
                    ),
                )
            if action == "execute":
                if not session_id:
                    return NodeResult(status=NodeStatus.FAILED, error="session_id 必填", summary="执行失败")
                cdp_action = str(params.get("cdp_action") or "navigate")
                payload_text = str(params.get("payload_text") or "")
                if cdp_action == "navigate" and url and not payload_text:
                    payload_text = url
                result = await asyncio.to_thread(
                    browser.execute,
                    session_id,
                    cdp_action,
                    str(params.get("target_node_id") or "") or None,
                    payload_text or None,
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=result,
                    summary=f"{cdp_action} 完成 url={result.get('url', '')}",
                )
            if action == "screenshot":
                if not session_id:
                    return NodeResult(status=NodeStatus.FAILED, error="session_id 必填", summary="截图失败")
                png = await asyncio.to_thread(browser.screenshot_png, session_id)
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"bytes": len(png), "session_id": session_id},
                    summary=f"截图 {len(png)} 字节",
                )
            if action == "close_session":
                if not session_id:
                    return NodeResult(status=NodeStatus.FAILED, error="session_id 必填", summary="关闭失败")
                await asyncio.to_thread(browser.close_session, session_id)
                return NodeResult(status=NodeStatus.SUCCESS, data={"session_id": session_id}, summary="会话已关闭")
            if action == "delete_profile":
                profile_id = str(params.get("profile_id") or "")
                if not profile_id:
                    return NodeResult(status=NodeStatus.FAILED, error="profile_id 必填", summary="删除失败")
                deleted = await asyncio.to_thread(browser.delete_profile, profile_id)
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"profile_id": profile_id, "deleted": deleted},
                    summary=f"profile {profile_id} {'已删除' if deleted else '不存在'}",
                )
            if action == "finish":
                result = await asyncio.to_thread(
                    browser.finish,
                    run_id,
                    not bool(params.get("keep_profile")),
                )
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data=result,
                    summary=f"收尾完成 (关闭 {len(result['closed_sessions'])} 会话)",
                )
            if action == "close":
                info = await asyncio.to_thread(browser.close)
                return NodeResult(status=NodeStatus.SUCCESS, data=info, summary="fusion-browser 已停止")
            return NodeResult(status=NodeStatus.FAILED, error=f"未知 action: {action}", summary="参数错误")
        except FusionBrowserError as e:
            logger.error(f"fusion-browser 节点失败 action={action}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="fusion-browser 失败")
        except Exception as e:
            logger.error(f"fusion-browser 节点异常 action={action}: {e}")
            return NodeResult(
                status=NodeStatus.FAILED, error=f"fusion-browser 异常: {e}", summary="fusion-browser 失败"
            )
