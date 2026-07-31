from __future__ import annotations

import logging
from typing import Any, Dict, List

from ...engine.node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus,
    NodeCategory, register_node, coerce_params,
)
from .cdp_client import CDPClient

logger = logging.getLogger(__name__)


class _CDPNodeBase(BaseNode):
    category = NodeCategory.TOOL

    def _get_client(self, inputs: Dict[str, Any], params: Dict[str, Any]) -> CDPClient:
        host = params.get("host", "127.0.0.1")
        port = params.get("port", 9222)
        client = CDPClient(host=host, port=int(port))
        return client


@register_node
class CDPNavigateNode(_CDPNodeBase):
    name = "cdp_navigate"
    display_name = "CDP 导航"
    description = "通过 Chrome CDP 导航到指定 URL"
    icon = "🌐"
    default_label = "CDP 导航"

    inputs = [{"key": "url", "label": "URL", "type": "string"}]
    outputs = [{"key": "frame_id", "label": "帧ID", "type": "string"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 URL")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            result = await client.navigate(url)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"url": url, "frame_id": result.get("frameId", "")},
                summary=f"已导航到: {url}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 导航失败")


@register_node
class CDPSnapshotNode(_CDPNodeBase):
    name = "cdp_snapshot"
    display_name = "CDP 快照"
    description = "获取页面无障碍树 (a11y tree)"
    icon = "🌳"
    default_label = "CDP 快照"

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
                summary=f"a11y tree: {len(nodes)} 节点",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 快照失败")


@register_node
class CDPClickNode(_CDPNodeBase):
    name = "cdp_click"
    display_name = "CDP 点击"
    description = "通过 CDP 点击页面元素"
    icon = "👆"
    default_label = "CDP 点击"

    inputs = [{"key": "backend_node_id", "label": "节点ID", "type": "integer"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 backend_node_id")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.click(int(node_id))
            await client.disconnect()
            if ok:
                return NodeResult(status=NodeStatus.SUCCESS, data={"clicked": True}, summary="点击成功")
            return NodeResult(status=NodeStatus.FAILED, error="点击失败", summary="点击失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 点击失败")


@register_node
class CDPFillNode(_CDPNodeBase):
    name = "cdp_fill"
    display_name = "CDP 填写"
    description = "通过 CSS 选择器填写表单字段"
    icon = "✍️"
    default_label = "CDP 填写"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少 selector 或 value")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.fill(selector, value)
            await client.disconnect()
            if ok:
                return NodeResult(status=NodeStatus.SUCCESS, data={"filled": True}, summary="填写成功")
            return NodeResult(status=NodeStatus.FAILED, error="填写失败", summary="填写失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 填写失败")


@register_node
class CDPFillFormNode(_CDPNodeBase):
    name = "cdp_fill_form"
    display_name = "CDP 批量填写"
    description = "批量填写多个表单字段"
    icon = "📝"
    default_label = "CDP 批量填写"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少 fields")
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
                summary=f"填写 {success_count}/{len(fields)} 个字段",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 批量填写失败")


@register_node
class CDPScreenshotNode(_CDPNodeBase):
    name = "cdp_screenshot"
    display_name = "CDP 截图"
    description = "通过 CDP 截取页面截图"
    icon = "📸"
    default_label = "CDP 截图"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
                    summary=f"截图已保存: {p} ({len(png_bytes)} bytes)",
                )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"size": len(png_bytes)},
                summary=f"截图完成 ({len(png_bytes)} bytes)",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 截图失败")


@register_node
class CDPEvaluateNode(_CDPNodeBase):
    name = "cdp_evaluate"
    display_name = "CDP 执行JS"
    description = "通过 CDP 在页面中执行 JavaScript"
    icon = "⚙️"
    default_label = "CDP 执行JS"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "JavaScript 代码"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["script"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        script = inputs.get("script", params.get("script", ""))
        if not script:
            return NodeResult(status=NodeStatus.FAILED, error="未指定 script")
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            value = await client.evaluate_js(script)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"result": value},
                summary="JS 执行成功",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP JS 执行失败")


@register_node
class CDPEmulateNode(_CDPNodeBase):
    name = "cdp_emulate"
    display_name = "CDP 设备模拟"
    description = "模拟设备视口/缩放"
    icon = "📱"
    default_label = "CDP 设备模拟"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "description": "视口宽度", "default": 1280},
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
                summary=f"视口已设为 {width}x{height}",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 设备模拟失败")


@register_node
class CDPNetworkNode(_CDPNodeBase):
    name = "cdp_network"
    display_name = "CDP 网络监控"
    description = "查询网络请求 (需要事件监听)"
    icon = "🔗"
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
                summary=f"网络请求: {len(requests)} 条",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 网络查询失败")


@register_node
class CDPConsoleNode(_CDPNodeBase):
    name = "cdp_console"
    display_name = "CDP 控制台"
    description = "查询控制台消息 (需要事件监听)"
    icon = "🖥️"
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
                summary=f"控制台消息: {len(messages)} 条",
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 控制台查询失败")
