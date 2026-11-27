from __future__ import annotations

import logging
from typing import Any, Dict

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)
from .cdp_client import CDPClient

logger = logging.getLogger(__name__)


class _CDPNodeBase(BaseNode):
    category = NodeCategory.TOOL

    def _get_client(self, inputs: Dict[str, Any], params: Dict[str, Any]) -> CDPClient:
        host = params.get("host", "127.0.0.1")
        port = params.get("port", 9222)
        token = params.get("token") or None
        client = CDPClient(host=host, port=int(port), token=token)
        return client


@register_node
class CDPNavigateNode(_CDPNodeBase):
    name = "cdp_navigate"
    # i18n: user-facing
    display_name = "CDP 导航"
    # i18n: user-facing
    description = "通过 Chrome CDP 导航到指定 URL"
    icon = "🌐"
    # i18n: user-facing
    default_label = "CDP 导航"

    inputs = [{"key": "url", "label": "URL", "type": "string"}]
    # i18n: user-facing
    outputs = [{"key": "frame_id", "label": "帧ID", "type": "string"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "url": {"type": "string", "description": "目标 URL"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["url"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = inputs.get("url", params.get("url", ""))
        if not url:
            return NodeResult(status=NodeStatus.FAILED, error="No URL specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            result = await client.navigate(url)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"url": url, "frame_id": result.get("frameId", "")},
                summary=f"Navigated to: {url}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP navigation failed")


@register_node
class CDPSnapshotNode(_CDPNodeBase):
    name = "cdp_snapshot"
    # i18n: user-facing
    display_name = "CDP 快照"
    # i18n: user-facing
    description = "获取页面无障碍树 (a11y tree)"
    icon = "🌳"
    # i18n: user-facing
    default_label = "CDP 快照"

    # i18n: user-facing
    outputs = [{"key": "tree", "label": "无障碍树", "type": "object"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            tree = await client.get_a11y_tree()
            await client.disconnect()
            nodes = tree.get("nodes", [])
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"tree": tree, "node_count": len(nodes)},
                summary=f"a11y tree: {len(nodes)} node(s)",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP snapshot failed")


@register_node
class CDPClickNode(_CDPNodeBase):
    name = "cdp_click"
    # i18n: user-facing
    display_name = "CDP 点击"
    # i18n: user-facing
    description = "通过 CDP 点击页面元素"
    icon = "👆"
    # i18n: user-facing
    default_label = "CDP 点击"

    # i18n: user-facing
    inputs = [{"key": "backend_node_id", "label": "节点ID", "type": "integer"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "backend_node_id": {"type": "integer", "description": "a11y tree 中的 backendNodeId"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["backend_node_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        node_id = inputs.get("backend_node_id", params.get("backend_node_id", 0))
        if not node_id:
            return NodeResult(status=NodeStatus.FAILED, error="No backend_node_id specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.click(int(node_id))
            await client.disconnect()
            if ok:
                return NodeResult(status=NodeStatus.SUCCESS, data={"clicked": True}, summary="Click succeeded")
            return NodeResult(status=NodeStatus.FAILED, error="Click failed", summary="Click failed")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP click failed")


@register_node
class CDPFillNode(_CDPNodeBase):
    name = "cdp_fill"
    # i18n: user-facing
    display_name = "CDP 填写"
    # i18n: user-facing
    description = "通过 CSS 选择器填写表单字段"
    icon = "✍️"
    # i18n: user-facing
    default_label = "CDP 填写"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "selector": {"type": "string", "description": "CSS 选择器"},
                # i18n: user-facing
                "value": {"type": "string", "description": "填写值"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["selector", "value"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        selector = inputs.get("selector", params.get("selector", ""))
        value = inputs.get("value", params.get("value", ""))
        if not selector or not value:
            return NodeResult(status=NodeStatus.FAILED, error="Missing selector or value")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.fill(selector, value)
            await client.disconnect()
            if ok:
                return NodeResult(status=NodeStatus.SUCCESS, data={"filled": True}, summary="Fill succeeded")
            return NodeResult(status=NodeStatus.FAILED, error="Fill failed", summary="Fill failed")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP fill failed")


@register_node
class CDPFillFormNode(_CDPNodeBase):
    name = "cdp_fill_form"
    # i18n: user-facing
    display_name = "CDP 批量填写"
    # i18n: user-facing
    description = "批量填写多个表单字段"
    icon = "📝"
    # i18n: user-facing
    default_label = "CDP 批量填写"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    # i18n: user-facing
                    "description": "表单字段列表 [{selector, value}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["fields"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        fields = inputs.get("fields", params.get("fields", []))
        if not fields:
            return NodeResult(status=NodeStatus.FAILED, error="Missing fields")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            results = []
            for field in fields:
                ok = await client.fill(field.get("selector", ""), field.get("value", ""))
                results.append({"selector": field.get("selector"), "success": ok})
            await client.disconnect()
            success_count = sum(1 for r in results if r["success"])
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"results": results, "success_count": success_count},
                summary=f"Filled {success_count}/{len(fields)} field(s)",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP batch fill failed")


@register_node
class CDPScreenshotNode(_CDPNodeBase):
    name = "cdp_screenshot"
    # i18n: user-facing
    display_name = "CDP 截图"
    # i18n: user-facing
    description = "通过 CDP 截取页面截图"
    icon = "📸"
    # i18n: user-facing
    default_label = "CDP 截图"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "save_path": {"type": "string", "description": "保存路径", "default": ""},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        save_path = inputs.get("save_path", params.get("save_path", ""))
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            png_bytes = await client.screenshot()
            await client.disconnect()
            if save_path:
                from pathlib import Path

                p = Path(save_path).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(png_bytes)
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"size": len(png_bytes), "path": str(p)},
                    summary=f"Screenshot saved: {p} ({len(png_bytes)} bytes)",
                )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"size": len(png_bytes)},
                summary=f"Screenshot done ({len(png_bytes)} bytes)",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP screenshot failed")


@register_node
class CDPEvaluateNode(_CDPNodeBase):
    name = "cdp_evaluate"
    # i18n: user-facing
    display_name = "CDP 执行JS"
    # i18n: user-facing
    description = "通过 CDP 在页面中执行 JavaScript"
    icon = "⚙️"
    # i18n: user-facing
    default_label = "CDP 执行JS"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "script": {"type": "string", "description": "JavaScript 代码"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
                "allow_js": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "CR-14: 显式确认放行任意 JS 执行 (高危)",
                },
            },
            "required": ["script"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        script = inputs.get("script", params.get("script", ""))
        if not script:
            return NodeResult(status=NodeStatus.FAILED, error="No script specified")
        # CR-14: arbitrary JS execution is high risk and requires explicit allow_js confirmation (Stage 3 will wire PermissionManager)
        if not params.get("allow_js", False):
            logger.error("cdp_evaluate rejected: allow_js=true not set (arbitrary JS execution is high risk, explicit confirmation required)")
            return NodeResult(
                status=NodeStatus.FAILED,
                error="CDP JS execution rejected: requires explicit confirmation (allow_js=true) + permission allow",
                summary="CDP JS execution unauthorized",
            )
        client = self._get_client(inputs, params)
        client.confirm_js_eval()
        try:
            await client.connect()
            value = await client.evaluate_js(script)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"result": value},
                summary="JS execution succeeded",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP JS execution failed")


@register_node
class CDPEmulateNode(_CDPNodeBase):
    name = "cdp_emulate"
    # i18n: user-facing
    display_name = "CDP 设备模拟"
    # i18n: user-facing
    description = "模拟设备视口/缩放"
    icon = "📱"
    # i18n: user-facing
    default_label = "CDP 设备模拟"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "width": {"type": "integer", "description": "视口宽度", "default": 1280},
                # i18n: user-facing
                "height": {"type": "integer", "description": "视口高度", "default": 720},
                "device_scale": {"type": "number", "default": 1.0},
                "mobile": {"type": "boolean", "default": False},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        width = int(params.get("width", 1280))
        height = int(params.get("height", 720))
        scale = float(params.get("device_scale", 1.0))
        mobile = params.get("mobile", False)
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.emulate_viewport(width, height, scale, mobile)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"width": width, "height": height, "mobile": mobile},
                summary=f"Viewport set to {width}x{height}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP device emulation failed")


@register_node
class CDPNetworkNode(_CDPNodeBase):
    name = "cdp_network"
    # i18n: user-facing
    display_name = "CDP 网络监控"
    # i18n: user-facing
    description = "查询网络请求 (需要事件监听)"
    icon = "🔗"
    # i18n: user-facing
    default_label = "CDP 网络"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            requests = await client.list_network_requests()
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"requests": requests, "count": len(requests)},
                summary=f"Network requests: {len(requests)}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP network query failed")


@register_node
class CDPConsoleNode(_CDPNodeBase):
    name = "cdp_console"
    # i18n: user-facing
    display_name = "CDP 控制台"
    # i18n: user-facing
    description = "查询控制台消息 (需要事件监听)"
    icon = "🖥️"
    # i18n: user-facing
    default_label = "CDP 控制台"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            messages = await client.list_console_messages()
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"messages": messages, "count": len(messages)},
                summary=f"Console messages: {len(messages)}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP console query failed")


@register_node
class CDPListPagesNode(_CDPNodeBase):
    name = "cdp_list_pages"
    # i18n: user-facing
    display_name = "CDP 页面列表"
    # i18n: user-facing
    description = "列出当前 Chrome 打开的所有页面 (tab)"
    icon = "📋"
    # i18n: user-facing
    default_label = "CDP 页面列表"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            pages = await client.list_pages()
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"pages": pages, "count": len(pages)}, summary=f"Pages: {len(pages)}"
            )
        except Exception as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP page list failed")


@register_node
class CDPSelectPageNode(_CDPNodeBase):
    name = "cdp_select_page"
    # i18n: user-facing
    display_name = "CDP 选择页面"
    # i18n: user-facing
    description = "切换 CDP 连接到指定 target_id 的页面"
    icon = "🎯"
    # i18n: user-facing
    default_label = "CDP 选择页面"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "target_id": {"type": "string", "description": "页面 target id"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["target_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        target_id = inputs.get("target_id", params.get("target_id", ""))
        if not target_id:
            return NodeResult(status=NodeStatus.FAILED, error="No target_id specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.select_page(target_id)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"selected": target_id}, summary=f"Switched to page: {target_id}"
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP select page failed")


@register_node
class CDPNewPageNode(_CDPNodeBase):
    name = "cdp_new_page"
    # i18n: user-facing
    display_name = "CDP 新建页面"
    # i18n: user-facing
    description = "通过 CDP HTTP 接口打开新页面 (tab)"
    icon = "➕"
    # i18n: user-facing
    default_label = "CDP 新建页面"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "default": "about:blank"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = inputs.get("url", params.get("url", "about:blank"))
        client = self._get_client(inputs, params)
        try:
            target = await client.new_page(url)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"target": target, "target_id": target.get("id", "")},
                summary=f"New page: {url}",
            )
        except Exception as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP new page failed")


@register_node
class CDPClosePageNode(_CDPNodeBase):
    name = "cdp_close_page"
    # i18n: user-facing
    display_name = "CDP 关闭页面"
    # i18n: user-facing
    description = "通过 target_id 关闭指定页面 (tab)"
    icon = "✖️"
    # i18n: user-facing
    default_label = "CDP 关闭页面"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "target_id": {"type": "string", "description": "页面 target id"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["target_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        target_id = inputs.get("target_id", params.get("target_id", ""))
        if not target_id:
            return NodeResult(status=NodeStatus.FAILED, error="No target_id specified")
        client = self._get_client(inputs, params)
        try:
            ok = await client.close_page(target_id)
            if ok:
                return NodeResult(
                    status=NodeStatus.SUCCESS, data={"closed": target_id}, summary=f"Page closed: {target_id}"
                )
            return NodeResult(status=NodeStatus.FAILED, error="Close failed", summary="CDP close page failed")
        except Exception as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP close page failed")


@register_node
class CDPResizePageNode(_CDPNodeBase):
    name = "cdp_resize_page"
    # i18n: user-facing
    display_name = "CDP 调整窗口"
    # i18n: user-facing
    description = "调整页面视口尺寸 (width x height)"
    icon = "📐"
    # i18n: user-facing
    default_label = "CDP 调整窗口"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 800},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        width = int(inputs.get("width", params.get("width", 1280)))
        height = int(inputs.get("height", params.get("height", 800)))
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.resize_page(width, height)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"width": width, "height": height}, summary=f"Window: {width}x{height}"
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP resize page failed")


@register_node
class CDPHoverNode(_CDPNodeBase):
    name = "cdp_hover"
    # i18n: user-facing
    display_name = "CDP 悬停"
    # i18n: user-facing
    description = "通过 CDP 悬停在指定元素上 (backendNodeId)"
    icon = "🖱️"
    # i18n: user-facing
    default_label = "CDP 悬停"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "backend_node_id": {"type": "integer", "description": "a11y tree backendNodeId"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["backend_node_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        node_id = inputs.get("backend_node_id", params.get("backend_node_id", 0))
        if not node_id:
            return NodeResult(status=NodeStatus.FAILED, error="No backend_node_id specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.hover(int(node_id))
            await client.disconnect()
            if ok:
                return NodeResult(status=NodeStatus.SUCCESS, data={"hovered": True}, summary="Hover succeeded")
            return NodeResult(status=NodeStatus.FAILED, error="Hover failed", summary="CDP hover failed")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP hover failed")


@register_node
class CDPDragNode(_CDPNodeBase):
    name = "cdp_drag"
    # i18n: user-facing
    display_name = "CDP 拖拽"
    # i18n: user-facing
    description = "通过 CDP 从起始坐标拖拽到目标坐标"
    icon = "🫳"
    # i18n: user-facing
    default_label = "CDP 拖拽"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_x": {"type": "number"},
                "start_y": {"type": "number"},
                "end_x": {"type": "number"},
                "end_y": {"type": "number"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        sx = float(inputs.get("start_x", params.get("start_x", 0)))
        sy = float(inputs.get("start_y", params.get("start_y", 0)))
        ex = float(inputs.get("end_x", params.get("end_x", 0)))
        ey = float(inputs.get("end_y", params.get("end_y", 0)))
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.drag(sx, sy, ex, ey)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS, data={"dragged": True}, summary=f"Drag: ({sx},{sy})->({ex},{ey})"
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP drag failed")


@register_node
class CDPTypeTextNode(_CDPNodeBase):
    name = "cdp_type_text"
    # i18n: user-facing
    display_name = "CDP 输入文本"
    # i18n: user-facing
    description = "向当前聚焦元素输入文本 (insertText)"
    icon = "⌨️"
    # i18n: user-facing
    default_label = "CDP 输入文本"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["text"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        text = inputs.get("text", params.get("text", ""))
        if not text:
            return NodeResult(status=NodeStatus.FAILED, error="No text specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.type_text(text)
            await client.disconnect()
            return NodeResult(status=NodeStatus.SUCCESS, data={"typed": True}, summary=f"Typed: {text[:30]}")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP type text failed")


@register_node
class CDPPressKeyNode(_CDPNodeBase):
    name = "cdp_press_key"
    # i18n: user-facing
    display_name = "CDP 按键"
    # i18n: user-facing
    description = "通过 CDP 发送按键事件 (key, 如 Enter/Tab/Escape)"
    icon = "🔑"
    # i18n: user-facing
    default_label = "CDP 按键"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["key"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        key = inputs.get("key", params.get("key", ""))
        if not key:
            return NodeResult(status=NodeStatus.FAILED, error="No key specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.press_key(key)
            await client.disconnect()
            return NodeResult(status=NodeStatus.SUCCESS, data={"pressed": key}, summary=f"Key pressed: {key}")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP press key failed")


@register_node
class CDPWaitForNode(_CDPNodeBase):
    name = "cdp_wait_for"
    # i18n: user-facing
    display_name = "CDP 等待条件"
    # i18n: user-facing
    description = "等待页面 JS 表达式为真 (轮询, 超时返回失败)"
    icon = "⏳"
    # i18n: user-facing
    default_label = "CDP 等待条件"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "expression": {"type": "string", "description": "JS 表达式, 为真时停止等待"},
                "timeout": {"type": "number", "default": 30.0},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
                "allow_js": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "E-10: 显式确认放行任意 JS 注入 (高危, 同 cdp_evaluate)",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        expression = inputs.get("expression", params.get("expression", ""))
        timeout = float(inputs.get("timeout", params.get("timeout", 30.0)))
        if not expression:
            return NodeResult(status=NodeStatus.FAILED, error="No expression specified")
        # E-10: wait_for_function executes arbitrary JS on the page, high risk like cdp_evaluate,
        # requires explicit allow_js confirmation + permission allow (cdp_wait_for is already in HIGH_RISK_NODES)
        if not params.get("allow_js", False):
            logger.error("cdp_wait_for rejected: allow_js=true not set (arbitrary JS injection is high risk, explicit confirmation required)")
            return NodeResult(
                status=NodeStatus.FAILED,
                error="CDP wait-for rejected: requires explicit confirmation (allow_js=true) + permission allow",
                summary="CDP wait-for unauthorized",
            )
        client = self._get_client(inputs, params)
        client.confirm_js_eval()
        try:
            await client.connect()
            result = await client.wait_for_function(expression, timeout=timeout)
            await client.disconnect()
            if result.get("success"):
                return NodeResult(status=NodeStatus.SUCCESS, data=result, summary="Condition met")
            return NodeResult(status=NodeStatus.FAILED, error="Wait timed out, condition not met", summary="CDP wait timed out")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP wait failed")


@register_node
class CDPHandleDialogNode(_CDPNodeBase):
    name = "cdp_handle_dialog"
    # i18n: user-facing
    display_name = "CDP 处理对话框"
    # i18n: user-facing
    description = "接受或拒绝 JS 弹窗 (alert/confirm/prompt)"
    icon = "💬"
    # i18n: user-facing
    default_label = "CDP 处理对话框"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "accept": {"type": "boolean", "default": True},
                "prompt_text": {"type": "string", "default": ""},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        accept = bool(inputs.get("accept", params.get("accept", True)))
        prompt_text = inputs.get("prompt_text", params.get("prompt_text", ""))
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.handle_dialog(accept, prompt_text)
            await client.disconnect()
            return NodeResult(status=NodeStatus.SUCCESS, data={"accept": accept}, summary="Dialog handled")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP handle dialog failed")


@register_node
class CDPUploadFileNode(_CDPNodeBase):
    name = "cdp_upload_file"
    # i18n: user-facing
    display_name = "CDP 上传文件"
    # i18n: user-facing
    description = "通过 CSS 选择器定位 file input 并上传本地文件"
    icon = "📤"
    # i18n: user-facing
    default_label = "CDP 上传文件"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "file_path": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["selector", "file_path"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        selector = inputs.get("selector", params.get("selector", ""))
        file_path = inputs.get("file_path", params.get("file_path", ""))
        if not selector or not file_path:
            return NodeResult(status=NodeStatus.FAILED, error="No selector or file_path specified")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.upload_file(selector, file_path)
            await client.disconnect()
            if ok:
                return NodeResult(
                    status=NodeStatus.SUCCESS, data={"uploaded": file_path}, summary=f"Uploaded: {file_path}"
                )
            return NodeResult(status=NodeStatus.FAILED, error="Upload failed", summary="CDP upload failed")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP upload failed")


@register_node
class CDPHeapSnapshotNode(_CDPNodeBase):
    name = "cdp_heapsnapshot"
    # i18n: user-facing
    display_name = "CDP 堆快照"
    # i18n: user-facing
    description = "采集 V8 堆快照 (HeapProfiler)"
    icon = "🧠"
    # i18n: user-facing
    default_label = "CDP 堆快照"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            snapshot = await client.take_heapsnapshot()
            await client.disconnect()
            return NodeResult(status=NodeStatus.SUCCESS, data={"snapshot_size": len(snapshot)}, summary="Heap snapshot captured")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP heap snapshot failed")


@register_node
class CDPPerformanceTraceNode(_CDPNodeBase):
    name = "cdp_performance_trace"
    # i18n: user-facing
    display_name = "CDP 性能追踪"
    # i18n: user-facing
    description = "启动性能追踪, 执行动作后获取性能指标 (Tracing + Performance)"
    icon = "📊"
    # i18n: user-facing
    default_label = "CDP 性能追踪"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "categories": {"type": "string", "default": "blink,devtools,cc,gpu,v8"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        categories = inputs.get("categories", params.get("categories", "blink,devtools,cc,gpu,v8"))
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.performance_trace_start(categories)
            await client.performance_trace_stop()
            await client.disconnect()
            return NodeResult(status=NodeStatus.SUCCESS, data={"traced": True}, summary="Performance trace done")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP performance trace failed")


@register_node
class CDPLighthouseNode(_CDPNodeBase):
    name = "cdp_lighthouse"
    # i18n: user-facing
    display_name = "CDP Lighthouse 审计"
    # i18n: user-facing
    description = "采集页面性能指标 (Lighthouse 替代; 需独立 lighthouse 进程做完整审计)"
    icon = "🚦"
    # i18n: user-facing
    default_label = "CDP Lighthouse 审计"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            metrics = await client.lighthouse_audit()
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"metrics": metrics},
                summary=f"Performance metrics: {len(metrics.get('metrics', []))}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP Lighthouse failed")
