from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import websockets

    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


class CDPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9222):
        self.host = host
        self.port = port
        self._ws = None
        self._msg_id = 0
        self._connected = False

    async def connect(self) -> None:
        if not HAS_HTTPX or not HAS_WEBSOCKETS:
            raise RuntimeError("CDP 需要 httpx 和 websockets 库: pip install httpx websockets")

        ws_url = await self._get_ws_url()
        self._ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
        self._connected = True
        logger.info(f"CDP 已连接: {ws_url}")

    async def _get_ws_url(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json")
            resp.raise_for_status()
            targets = resp.json()
            if not targets:
                raise ConnectionError("没有可用的 Chrome 调试目标")
            page = targets[0]
            ws_url = page.get("webSocketDebuggerUrl")
            if not ws_url:
                raise ConnectionError("无法获取 WebSocket 调试 URL")
            return ws_url

    async def disconnect(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("CDP 已断开")

    async def send(
        self,
        method: str,
        params: Dict[str, Any] = None,
        max_retries: int = 100,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        if not self._connected or self._ws is None:
            raise RuntimeError("CDP 未连接")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))
        for _ in range(max_retries):
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                return data
        raise RuntimeError(f"CDP send() 超过最大重试次数 ({max_retries}): method={method}")

    @property
    def connected(self) -> bool:
        return self._connected

    async def navigate(self, url: str) -> Dict[str, Any]:
        result = await self.send("Page.navigate", {"url": url})
        logger.info(f"CDP 导航: {url}")
        return result.get("result", {})

    async def get_a11y_tree(self) -> Dict[str, Any]:
        result = await self.send("Accessibility.getFullAXTree")
        return result.get("result", {})

    async def click(self, backend_node_id: int) -> bool:
        try:
            resolve = await self.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
            obj_id = resolve.get("result", {}).get("object", {}).get("objectId")
            if not obj_id:
                return False
            await self.send("DOM.focus", {"objectId": obj_id})
            box = await self.send("DOM.getBoxModel", {"objectId": obj_id})
            model = box.get("result", {}).get("model", {})
            content = model.get("content", [])
            if len(content) >= 8:
                x = (content[0] + content[2] + content[4] + content[6]) / 4
                y = (content[1] + content[3] + content[5] + content[7]) / 4
            else:
                return False
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            logger.info(f"CDP 点击: node={backend_node_id}, x={x:.0f}, y={y:.0f}")
            return True
        except Exception as e:
            logger.error(f"CDP 点击失败: {e}")
            return False

    async def fill(self, selector: str, value: str) -> bool:
        try:
            doc = await self.send("DOM.getDocument")
            root_id = doc.get("result", {}).get("root", {}).get("nodeId", 0)
            query = await self.send(
                "DOM.querySelector",
                {
                    "nodeId": root_id,
                    "selector": selector,
                },
            )
            node_id = query.get("result", {}).get("nodeId", 0)
            if node_id == 0:
                return False
            await self.send("DOM.focus", {"nodeId": node_id})
            await self.send("Input.insertText", {"text": value})
            logger.info(f"CDP 填写: selector={selector}, value={value[:30]}")
            return True
        except Exception as e:
            logger.error(f"CDP 填写失败: {e}")
            return False

    async def screenshot(self) -> bytes:
        import base64

        result = await self.send("Page.captureScreenshot", {"format": "png"})
        data_b64 = result.get("result", {}).get("data", "")
        logger.info(f"CDP 截图: {len(data_b64)} bytes base64")
        return base64.b64decode(data_b64)

    async def evaluate_js(self, script: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": script,
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("result", {}).get("value")
        logger.info(f"CDP 执行JS: {script[:60]}")
        return value

    async def emulate_viewport(self, width: int, height: int, device_scale: float = 1.0, mobile: bool = False) -> None:
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": device_scale,
                "mobile": mobile,
            },
        )
        logger.info(f"CDP 模拟视口: {width}x{height}")

    async def list_network_requests(self) -> List[Dict[str, Any]]:
        logger.info("CDP 网络请求查询 (需要事件监听)")
        return []

    async def list_console_messages(self) -> List[Dict[str, Any]]:
        logger.info("CDP 控制台消息查询 (需要事件监听)")
        return []
