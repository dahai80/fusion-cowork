from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# CR-14: navigate only allows http/https, rejects dangerous schemes (file/javascript/chrome/data/about)
_ALLOWED_NAV_SCHEMES = {"http", "https"}

# issue #65: fusion-browser CDP-over-WebSocket compatibility-layer target switch
# FUSION_BROWSER_CDP=<port> set -> connect to the fusion-browser shim at 127.0.0.1:<port> (not real Chrome)
# unset -> use the existing external Chrome path (9222 debug port), behavior unchanged
_FUSION_BROWSER_CDP_ENV = "FUSION_BROWSER_CDP"

# issue #77: fusion-browser E-15 fail-closed Origin gate — the WS upgrade must send an allowlisted Origin
# FUSION_CDP_ORIGIN=<origin> (e.g. https://fusion.local) must match the fusion-browser allowedOrigins config
# rejected when the Origin header is absent (an empty allowlist only lets through local data:/about:/blob: schemes)
_FUSION_CDP_ORIGIN_ENV = "FUSION_CDP_ORIGIN"


def _resolve_fusion_browser_port() -> Optional[int]:
    raw = os.environ.get(_FUSION_BROWSER_CDP_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning(f"{_FUSION_BROWSER_CDP_ENV}={raw!r} is not a valid port, ignoring, using the default Chrome path")
        return None
    if not (1 <= port <= 65535):
        logger.warning(f"{_FUSION_BROWSER_CDP_ENV}={port} is out of port range, ignoring")
        return None
    return port


def _resolve_cdp_origin() -> Optional[str]:
    raw = os.environ.get(_FUSION_CDP_ORIGIN_ENV, "").strip()
    return raw or None


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
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9222,
        token: Optional[str] = None,
        origin: Optional[str] = None,
    ):
        # issue #65: FUSION_BROWSER_CDP set -> force-switch to the fusion-browser shim target
        # issue #72: the fusion-browser WS upgrade fail-closed requires Authorization: Bearer,
        # so the fusion-browser path keeps the token (not set to None) and forwards it on connect() to the WS upgrade
        fb_port = _resolve_fusion_browser_port()
        if fb_port is not None:
            self.host = "127.0.0.1"
            self.port = fb_port
            self.token = token
            self._target = "fusion-browser"
            logger.info(f"CDP target=fusion-browser shim (env {_FUSION_BROWSER_CDP_ENV}={fb_port})")
        else:
            self.host = host
            self.port = port
            self.token = token
            self._target = "chrome"
        # issue #77: fusion-browser E-15 fail-closed Origin gate — the constructor origin takes priority,
        # defaulting back to the FUSION_CDP_ORIGIN env. The Chrome path does not strictly need Origin, but sending it is harmless.
        self.origin = origin or _resolve_cdp_origin()
        if self._target == "fusion-browser" and not self.origin:
            logger.warning(
                f"fusion-browser E-15 Origin gate fail-closed: {_FUSION_CDP_ORIGIN_ENV} unset, "
                "WS upgrade / Page.navigate / PUT /json/new will be rejected (must match allowedOrigins)"
            )
        self._ws = None
        self._msg_id = 0
        self._connected = False
        self._reader_task: asyncio.Task | None = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._network_buffer: List[Dict[str, Any]] = []
        self._console_buffer: List[Dict[str, Any]] = []
        self._network_enabled = False
        self._console_enabled = False
        self._js_eval_confirmed = False  # CR-14: evaluate_js requires explicit confirmation
        # CR-14: localhost only by default; a non-localhost connection to 9222 without auth = high risk, rejected
        # the fusion-browser path forces 127.0.0.1 and skips this check
        if self._target == "chrome" and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"CDP host={self.host} is not local, refusing to connect (the 9222 debug port is unauthenticated, localhost only)")
        if self._target == "chrome" and not self.token:
            logger.warning("CDP 9222 debug port has no token configured, connection is unauthenticated (trusted local environment only)")

    async def _ws_connect(self, ws_url: str):
        # issue #72: the fusion-browser / Chrome CDP WS upgrade requires Authorization: Bearer
        # forward self.token (same source as the /json GET); omit when there is no token (Chrome 9222 debug-port compatibility)
        # issue #77: fusion-browser E-15 fail-closed Origin gate — must send an allowlisted Origin,
        # otherwise the WS upgrade is rejected with 403. Origin and Bearer are sent together (two independent gates, both must pass)
        # websockets >=12 uses additional_headers, older versions use extra_headers — probed for compatibility
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.origin:
            headers["Origin"] = self.origin
        if headers:
            try:
                return await websockets.connect(ws_url, max_size=10 * 1024 * 1024, additional_headers=headers)
            except TypeError:
                return await websockets.connect(ws_url, max_size=10 * 1024 * 1024, extra_headers=headers)
        return await websockets.connect(ws_url, max_size=10 * 1024 * 1024)

    async def connect(self) -> None:
        if not HAS_HTTPX or not HAS_WEBSOCKETS:
            raise RuntimeError("CDP requires the httpx and websockets libraries: pip install httpx websockets")

        ws_url = await self._get_ws_url()
        self._ws = await self._ws_connect(ws_url)
        self._connected = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info(f"CDP connected: {ws_url}")

    def confirm_js_eval(self) -> None:
        # CR-14: the caller (node layer, after permission confirmation) explicitly allows evaluate_js
        self._js_eval_confirmed = True
        logger.warning("CDP evaluate_js explicitly confirmed and allowed (high-risk operation)")

    async def _reader_loop(self) -> None:
        # Background WS reader: messages without an id go to event buffers, messages with an id match pending futures
        try:
            while self._connected and self._ws is not None:
                raw = await self._ws.recv()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_id = data.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    self._dispatch_event(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"CDP reader_loop exited: {e}")

    def _dispatch_event(self, data: Dict[str, Any]) -> None:
        method = data.get("method", "")
        params = data.get("params", {})
        if method.startswith("Network."):
            self._network_buffer.append({"method": method, **params})
            if len(self._network_buffer) > 1000:
                self._network_buffer = self._network_buffer[-1000:]
        elif method == "Runtime.consoleAPICalled":
            self._console_buffer.append({"method": method, **params})
            if len(self._console_buffer) > 500:
                self._console_buffer = self._console_buffer[-500:]

    async def _get_ws_url(self) -> str:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json", headers=headers)
            resp.raise_for_status()
            targets = resp.json()
            if not targets:
                raise ConnectionError("No Chrome debug targets available")
            page = targets[0]
            ws_url = page.get("webSocketDebuggerUrl")
            if not ws_url:
                raise ConnectionError("Cannot obtain the WebSocket debug URL")
            return ws_url

    async def disconnect(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        self._pending.clear()
        logger.info("CDP disconnected")

    async def send(
        self,
        method: str,
        params: Dict[str, Any] = None,
        max_retries: int = 100,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        if not self._connected or self._ws is None:
            raise RuntimeError("CDP not connected")
        self._msg_id += 1
        msg_id = self._msg_id
        msg = {"id": msg_id, "method": method, "params": params or {}}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP send() timed out ({timeout}s): method={method}") from None

    @property
    def connected(self) -> bool:
        return self._connected

    async def navigate(self, url: str) -> Dict[str, Any]:
        # CR-14: validate the scheme, only http/https allowed, reject file/javascript/chrome/data etc.
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if not scheme:
            raise ValueError(f"CDP navigation rejected: URL missing scheme: {url!r}")
        if scheme not in _ALLOWED_NAV_SCHEMES:
            logger.warning(f"CDP navigation rejected: scheme={scheme!r} not allowed (http/https only): {url!r}")
            raise ValueError(f"CDP navigation rejected: scheme={scheme!r} not in the allowlist (http/https only)")
        result = await self.send("Page.navigate", {"url": url})
        logger.info(f"CDP navigate: {url}")
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
            logger.info(f"CDP click: node={backend_node_id}, x={x:.0f}, y={y:.0f}")
            return True
        except Exception as e:
            logger.error(f"CDP click failed: {e}")
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
            logger.info(f"CDP fill: selector={selector}, value={value[:30]}")
            return True
        except Exception as e:
            logger.error(f"CDP fill failed: {e}")
            return False

    async def screenshot(self) -> bytes:
        import base64

        result = await self.send("Page.captureScreenshot", {"format": "png"})
        data_b64 = result.get("result", {}).get("data", "")
        logger.info(f"CDP screenshot: {len(data_b64)} bytes base64")
        return base64.b64decode(data_b64)

    async def evaluate_js(self, script: str) -> Any:
        # CR-14: arbitrary JS execution = high risk; the caller must confirm permissions, then confirm_js_eval() allows it
        if not self._js_eval_confirmed:
            logger.error("CDP evaluate_js rejected: not explicitly confirmed (requires confirm_js_eval() to allow)")
            raise PermissionError("CDP evaluate_js rejected: arbitrary JS execution is high risk, requires permission confirmation then confirm_js_eval()")
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": script,
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("result", {}).get("value")
        logger.info(f"CDP execute JS: {script[:60]}")
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
        logger.info(f"CDP emulate viewport: {width}x{height}")

    async def enable_network(self) -> None:
        if not self._network_enabled:
            await self.send("Network.enable")
            self._network_enabled = True
            self._network_buffer.clear()
            logger.info("CDP Network event listening enabled")

    async def enable_console(self) -> None:
        if not self._console_enabled:
            await self.send("Runtime.enable")
            self._console_enabled = True
            self._console_buffer.clear()
            logger.info("CDP Runtime console listening enabled")

    async def list_network_requests(self) -> List[Dict[str, Any]]:
        await self.enable_network()
        # Brief wait to collect the latest events
        await asyncio.sleep(0.1)
        return list(self._network_buffer)

    async def list_console_messages(self) -> List[Dict[str, Any]]:
        await self.enable_console()
        await asyncio.sleep(0.1)
        return list(self._console_buffer)

    async def list_pages(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json")
            resp.raise_for_status()
            targets = resp.json()
        pages = [t for t in targets if t.get("type") == "page"]
        logger.info(f"CDP page list: {len(pages)} page(s)")
        return pages

    async def new_page(self, url: str = "about:blank") -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.put(f"http://{self.host}:{self.port}/json/new?{url}")
            resp.raise_for_status()
            target = resp.json()
        logger.info(f"CDP new page: {target.get('id', '?')} -> {url}")
        return target

    async def close_page(self, target_id: str) -> bool:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json/close/{target_id}")
            ok = resp.status_code == 200
        logger.info(f"CDP close page: {target_id} ok={ok}")
        return ok

    async def select_page(self, target_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json")
            resp.raise_for_status()
            targets = resp.json()
        target = next((t for t in targets if t.get("id") == target_id), None)
        if not target:
            raise RuntimeError(f"Page not found: {target_id}")
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Cannot obtain the target WebSocket URL")
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._ws = await self._ws_connect(ws_url)
        logger.info(f"CDP select page: {target_id}")

    async def resize_page(self, width: int, height: int) -> None:
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        logger.info(f"CDP resize page: {width}x{height}")

    async def mouse_move(self, x: float, y: float) -> None:
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        logger.info(f"CDP mouse move: x={x:.0f}, y={y:.0f}")

    async def hover(self, backend_node_id: int) -> bool:
        try:
            resolve = await self.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
            obj_id = resolve.get("result", {}).get("object", {}).get("objectId")
            if not obj_id:
                return False
            box = await self.send("DOM.getBoxModel", {"objectId": obj_id})
            content = box.get("result", {}).get("model", {}).get("content", [])
            if len(content) < 8:
                return False
            x = (content[0] + content[2] + content[4] + content[6]) / 4
            y = (content[1] + content[3] + content[5] + content[7]) / 4
            await self.mouse_move(x, y)
            logger.info(f"CDP hover: node={backend_node_id}")
            return True
        except Exception as e:
            logger.error(f"CDP hover failed: {e}")
            return False

    async def drag(self, start_x: float, start_y: float, end_x: float, end_y: float) -> None:
        for kind in ("mousePressed", "mouseMoved", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": start_x if kind == "mousePressed" else end_x,
                    "y": start_y if kind == "mousePressed" else end_y,
                    "button": "left",
                    "clickCount": 1,
                },
            )
        logger.info(f"CDP drag: ({start_x:.0f},{start_y:.0f}) -> ({end_x:.0f},{end_y:.0f})")

    async def type_text(self, text: str) -> None:
        await self.send("Input.insertText", {"text": text})
        logger.info(f"CDP type text: {text[:30]}")

    async def press_key(self, key: str) -> None:
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        logger.info(f"CDP press key: {key}")

    async def wait_for_function(self, expression: str, timeout: float = 30.0, polling: int = 500) -> Dict[str, Any]:
        # E-10: expression injection defense — rejects dangerous markers (}/ ; and backticks), prevents truncation setInterval/Promise context escape
        _dangerous = ("}", ";", "`", "eval(", "Function(", "=>{", "new Function")
        for tok in _dangerous:
            if tok in expression:
                logger.error(f"CDP wait_for_function rejected: expression contains dangerous marker '{tok}'")
                raise ValueError(f"CDP wait_for_function expression contains dangerous marker '{tok}', injection refused")
        await self.send("Runtime.enable")
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": f"new Promise((resolve)=>{{const t=setInterval(()=>{{try{{if({expression}){{clearInterval(t);resolve(true);}}}}catch(e){{}}}}, {polling});setTimeout(()=>{{clearInterval(t);resolve(false);}}, {int(timeout * 1000)});}})",
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        value = result.get("result", {}).get("result", {}).get("value", False)
        logger.info(f"CDP wait for condition: ok={value}")
        return {"success": bool(value)}

    async def handle_dialog(self, accept: bool = True, prompt_text: str = "") -> None:
        await self.send("Page.handleJavaScriptDialog", {"accept": accept, "promptText": prompt_text})
        logger.info(f"CDP handle dialog: accept={accept}")

    async def upload_file(self, selector: str, file_path: str) -> bool:
        try:
            doc = await self.send("DOM.getDocument")
            root_id = doc.get("result", {}).get("root", {}).get("nodeId", 0)
            query = await self.send("DOM.querySelector", {"nodeId": root_id, "selector": selector})
            node_id = query.get("result", {}).get("nodeId", 0)
            if node_id == 0:
                return False
            await self.send("DOM.setFileInputFiles", {"nodeId": node_id, "files": [file_path]})
            logger.info(f"CDP upload file: {file_path} -> {selector}")
            return True
        except Exception as e:
            logger.error(f"CDP upload failed: {e}")
            return False

    async def take_heapsnapshot(self) -> str:
        result = await self.send("HeapProfiler.takeHeapSnapshot", {"reportProgress": False})
        logger.info("CDP heap snapshot captured")
        return json.dumps(result.get("result", {}))

    async def performance_trace_start(self, categories: str = "blink,devtools,cc,gpu,v8") -> None:
        await self.send("Performance.enable")
        await self.send(
            "Tracing.start", {"traceConfig": {"includedCategories": categories.split(","), "excludedCategories": []}}
        )
        logger.info(f"CDP performance trace started: {categories}")

    async def performance_trace_stop(self) -> Dict[str, Any]:
        await self.send("Tracing.end")
        metrics = await self.send("Performance.getMetrics")
        logger.info("CDP performance trace stopped")
        return metrics.get("result", {})

    async def lighthouse_audit(self) -> Dict[str, Any]:
        logger.warning("CDP lighthouse is not a native CDP capability — returns performance metrics instead (a separate lighthouse process is needed)")
        await self.send("Performance.enable")
        metrics = await self.send("Performance.getMetrics")
        return metrics.get("result", {})
