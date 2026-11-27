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
    display_name = "CDP 导航"  # i18n: user-facing
    description = "通过 Chrome CDP 导航到指定 URL"  # i18n: user-facing
    icon = "🌐"
    default_label = "CDP 导航"  # i18n: user-facing

    inputs = [{"key": "url", "label": "URL", "type": "string"}]
    outputs = [{"key": "frame_id", "label": "帧ID", "type": "string"}]  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},  # i18n: user-facing
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["url"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = inputs.get("url", params.get("url", ""))
        if not url:
            return NodeResult(status=NodeStatus.FAILED, error="未指定 URL")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            result = await client.navigate(url)
            await client.disconnect()
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"url": url, "frame_id": result.get("frameId", "")},
                summary=f"已导航到: {url}",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 导航失败")  # i18n: user-facing


@register_node
class CDPSnapshotNode(_CDPNodeBase):
    name = "cdp_snapshot"
    display_name = "CDP 快照"  # i18n: user-facing
    description = "获取页面无障碍树 (a11y tree)"  # i18n: user-facing
    icon = "🌳"
    default_label = "CDP 快照"  # i18n: user-facing

    outputs = [{"key": "tree", "label": "无障碍树", "type": "object"}]  # i18n: user-facing

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
                summary=f"a11y tree: {len(nodes)} 节点",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 快照失败")  # i18n: user-facing


@register_node
class CDPClickNode(_CDPNodeBase):
    name = "cdp_click"
    display_name = "CDP 点击"  # i18n: user-facing
    description = "通过 CDP 点击页面元素"  # i18n: user-facing
    icon = "👆"
    default_label = "CDP 点击"  # i18n: user-facing

    inputs = [{"key": "backend_node_id", "label": "节点ID", "type": "integer"}]  # i18n: user-facing

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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 backend_node_id")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.click(int(node_id))
            await client.disconnect()
            if ok:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.SUCCESS, data={"clicked": True}, summary="点击成功")
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="点击失败", summary="点击失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 点击失败")  # i18n: user-facing


@register_node
class CDPFillNode(_CDPNodeBase):
    name = "cdp_fill"
    display_name = "CDP 填写"  # i18n: user-facing
    description = "通过 CSS 选择器填写表单字段"  # i18n: user-facing
    icon = "✍️"
    default_label = "CDP 填写"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},  # i18n: user-facing
                "value": {"type": "string", "description": "填写值"},  # i18n: user-facing
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少 selector 或 value")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.fill(selector, value)
            await client.disconnect()
            if ok:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.SUCCESS, data={"filled": True}, summary="填写成功")
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="填写失败", summary="填写失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 填写失败")  # i18n: user-facing


@register_node
class CDPFillFormNode(_CDPNodeBase):
    name = "cdp_fill_form"
    display_name = "CDP 批量填写"  # i18n: user-facing
    description = "批量填写多个表单字段"  # i18n: user-facing
    icon = "📝"
    default_label = "CDP 批量填写"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "description": "表单字段列表 [{selector, value}]",  # i18n: user-facing
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少 fields")  # i18n: user-facing
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
                summary=f"填写 {success_count}/{len(fields)} 个字段",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 批量填写失败")


@register_node
class CDPScreenshotNode(_CDPNodeBase):
    name = "cdp_screenshot"
    display_name = "CDP 截图"  # i18n: user-facing
    description = "通过 CDP 截取页面截图"  # i18n: user-facing
    icon = "📸"
    default_label = "CDP 截图"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "save_path": {"type": "string", "description": "保存路径", "default": ""},  # i18n: user-facing
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
                    summary=f"截图已保存: {p} ({len(png_bytes)} bytes)",  # i18n: user-facing
                )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"size": len(png_bytes)},
                summary=f"截图完成 ({len(png_bytes)} bytes)",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 截图失败")  # i18n: user-facing


@register_node
class CDPEvaluateNode(_CDPNodeBase):
    name = "cdp_evaluate"
    display_name = "CDP 执行JS"  # i18n: user-facing
    description = "通过 CDP 在页面中执行 JavaScript"  # i18n: user-facing
    icon = "⚙️"
    default_label = "CDP 执行JS"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "JavaScript 代码"},  # i18n: user-facing
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
                "allow_js": {
                    "type": "boolean",
                    "default": False,
                    "description": "CR-14: 显式确认放行任意 JS 执行 (高危)",  # i18n: user-facing
                },
            },
            "required": ["script"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        script = inputs.get("script", params.get("script", ""))
        if not script:
            return NodeResult(status=NodeStatus.FAILED, error="未指定 script")  # i18n: user-facing
        # CR-14: arbitrary JS execution is high-risk; requires explicit allow_js confirmation (PermissionManager wiring in Stage 3)
        if not params.get("allow_js", False):
            logger.error(
                "cdp_evaluate denied: allow_js=true not set (arbitrary JS is high-risk, explicit confirmation required)"
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                error="CDP 执行 JS 被拒绝: 须显式确认 (allow_js=true) + 权限放行",  # i18n: user-facing
                summary="CDP JS 执行未授权",  # i18n: user-facing
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
                summary="JS 执行成功",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP JS 执行失败")


@register_node
class CDPEmulateNode(_CDPNodeBase):
    name = "cdp_emulate"
    display_name = "CDP 设备模拟"  # i18n: user-facing
    description = "模拟设备视口/缩放"  # i18n: user-facing
    icon = "📱"
    default_label = "CDP 设备模拟"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "description": "视口宽度", "default": 1280},  # i18n: user-facing
                "height": {"type": "integer", "description": "视口高度", "default": 720},  # i18n: user-facing
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
                summary=f"视口已设为 {width}x{height}",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 设备模拟失败")


@register_node
class CDPNetworkNode(_CDPNodeBase):
    name = "cdp_network"
    display_name = "CDP 网络监控"  # i18n: user-facing
    description = "查询网络请求 (需要事件监听)"  # i18n: user-facing
    icon = "🔗"
    default_label = "CDP 网络"  # i18n: user-facing

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
                summary=f"网络请求: {len(requests)} 条",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 网络查询失败")


@register_node
class CDPConsoleNode(_CDPNodeBase):
    name = "cdp_console"
    display_name = "CDP 控制台"  # i18n: user-facing
    description = "查询控制台消息 (需要事件监听)"  # i18n: user-facing
    icon = "🖥️"
    default_label = "CDP 控制台"  # i18n: user-facing

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
                summary=f"控制台消息: {len(messages)} 条",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 控制台查询失败")


@register_node
class CDPListPagesNode(_CDPNodeBase):
    name = "cdp_list_pages"
    display_name = "CDP 页面列表"  # i18n: user-facing
    description = "列出当前 Chrome 打开的所有页面 (tab)"  # i18n: user-facing
    icon = "📋"
    default_label = "CDP 页面列表"  # i18n: user-facing

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
                # i18n: user-facing
                status=NodeStatus.SUCCESS,
                data={"pages": pages, "count": len(pages)},
                summary=f"页面: {len(pages)} 个",  # i18n: user-facing
            )
        except Exception as e:
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 页面列表失败")


@register_node
class CDPSelectPageNode(_CDPNodeBase):
    name = "cdp_select_page"
    display_name = "CDP 选择页面"  # i18n: user-facing
    description = "切换 CDP 连接到指定 target_id 的页面"  # i18n: user-facing
    icon = "🎯"
    default_label = "CDP 选择页面"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "页面 target id"},  # i18n: user-facing
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["target_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        target_id = inputs.get("target_id", params.get("target_id", ""))
        if not target_id:
            return NodeResult(status=NodeStatus.FAILED, error="未指定 target_id")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.select_page(target_id)
            await client.disconnect()
            return NodeResult(
                # i18n: user-facing
                status=NodeStatus.SUCCESS,
                data={"selected": target_id},
                summary=f"已切换到页面: {target_id}",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 切换页面失败")


@register_node
class CDPNewPageNode(_CDPNodeBase):
    name = "cdp_new_page"
    display_name = "CDP 新建页面"  # i18n: user-facing
    description = "通过 CDP HTTP 接口打开新页面 (tab)"  # i18n: user-facing
    icon = "➕"
    default_label = "CDP 新建页面"  # i18n: user-facing

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
                summary=f"新页面: {url}",  # i18n: user-facing
            )
        except Exception as e:
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 新建页面失败")


@register_node
class CDPClosePageNode(_CDPNodeBase):
    name = "cdp_close_page"
    display_name = "CDP 关闭页面"  # i18n: user-facing
    description = "通过 target_id 关闭指定页面 (tab)"  # i18n: user-facing
    icon = "✖️"
    default_label = "CDP 关闭页面"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "页面 target id"},  # i18n: user-facing
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 9222},
            },
            "required": ["target_id"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        target_id = inputs.get("target_id", params.get("target_id", ""))
        if not target_id:
            return NodeResult(status=NodeStatus.FAILED, error="未指定 target_id")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            ok = await client.close_page(target_id)
            if ok:
                return NodeResult(
                    # i18n: user-facing
                    status=NodeStatus.SUCCESS,
                    data={"closed": target_id},
                    summary=f"已关闭页面: {target_id}",  # i18n: user-facing
                )
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="关闭失败", summary="CDP 关闭页面失败")
        except Exception as e:
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 关闭页面失败")


@register_node
class CDPResizePageNode(_CDPNodeBase):
    name = "cdp_resize_page"
    display_name = "CDP 调整窗口"  # i18n: user-facing
    description = "调整页面视口尺寸 (width x height)"  # i18n: user-facing
    icon = "📐"
    default_label = "CDP 调整窗口"  # i18n: user-facing

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
                # i18n: user-facing
                status=NodeStatus.SUCCESS,
                data={"width": width, "height": height},
                summary=f"窗口: {width}x{height}",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 调整窗口失败")


@register_node
class CDPHoverNode(_CDPNodeBase):
    name = "cdp_hover"
    display_name = "CDP 悬停"  # i18n: user-facing
    description = "通过 CDP 悬停在指定元素上 (backendNodeId)"  # i18n: user-facing
    icon = "🖱️"
    default_label = "CDP 悬停"  # i18n: user-facing

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 backend_node_id")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.hover(int(node_id))
            await client.disconnect()
            if ok:
                # i18n: user-facing
                return NodeResult(status=NodeStatus.SUCCESS, data={"hovered": True}, summary="悬停成功")
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="悬停失败", summary="CDP 悬停失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 悬停失败")  # i18n: user-facing


@register_node
class CDPDragNode(_CDPNodeBase):
    name = "cdp_drag"
    display_name = "CDP 拖拽"  # i18n: user-facing
    description = "通过 CDP 从起始坐标拖拽到目标坐标"  # i18n: user-facing
    icon = "🫳"
    default_label = "CDP 拖拽"  # i18n: user-facing

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
                # i18n: user-facing
                status=NodeStatus.SUCCESS,
                data={"dragged": True},
                summary=f"拖拽: ({sx},{sy})->({ex},{ey})",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 拖拽失败")  # i18n: user-facing


@register_node
class CDPTypeTextNode(_CDPNodeBase):
    name = "cdp_type_text"
    display_name = "CDP 输入文本"  # i18n: user-facing
    description = "向当前聚焦元素输入文本 (insertText)"  # i18n: user-facing
    icon = "⌨️"
    default_label = "CDP 输入文本"  # i18n: user-facing

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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 text")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.type_text(text)
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data={"typed": True}, summary=f"已输入: {text[:30]}")
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 输入文本失败")


@register_node
class CDPPressKeyNode(_CDPNodeBase):
    name = "cdp_press_key"
    display_name = "CDP 按键"  # i18n: user-facing
    description = "通过 CDP 发送按键事件 (key, 如 Enter/Tab/Escape)"  # i18n: user-facing
    icon = "🔑"
    default_label = "CDP 按键"  # i18n: user-facing

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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 key")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            await client.press_key(key)
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data={"pressed": key}, summary=f"按键: {key}")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 按键失败")  # i18n: user-facing


@register_node
class CDPWaitForNode(_CDPNodeBase):
    name = "cdp_wait_for"
    display_name = "CDP 等待条件"  # i18n: user-facing
    description = "等待页面 JS 表达式为真 (轮询, 超时返回失败)"  # i18n: user-facing
    icon = "⏳"
    default_label = "CDP 等待条件"  # i18n: user-facing

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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 expression")  # i18n: user-facing
        # E-10: wait_for_function executes arbitrary JS in the page, same high risk as cdp_evaluate;
        # requires explicit allow_js confirmation + permission approval (cdp_wait_for is already in HIGH_RISK_NODES)
        if not params.get("allow_js", False):
            logger.error(
                "cdp_wait_for denied: allow_js=true not set (arbitrary JS injection is high-risk, confirmation required)"
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                error="CDP 等待条件被拒绝: 须显式确认 (allow_js=true) + 权限放行",  # i18n: user-facing
                summary="CDP 等待条件未授权",  # i18n: user-facing
            )
        client = self._get_client(inputs, params)
        client.confirm_js_eval()
        try:
            await client.connect()
            result = await client.wait_for_function(expression, timeout=timeout)
            await client.disconnect()
            if result.get("success"):
                return NodeResult(status=NodeStatus.SUCCESS, data=result, summary="条件满足")  # i18n: user-facing
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="等待超时, 条件未满足", summary="CDP 等待超时")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 等待失败")  # i18n: user-facing


@register_node
class CDPHandleDialogNode(_CDPNodeBase):
    name = "cdp_handle_dialog"
    display_name = "CDP 处理对话框"  # i18n: user-facing
    description = "接受或拒绝 JS 弹窗 (alert/confirm/prompt)"  # i18n: user-facing
    icon = "💬"
    default_label = "CDP 处理对话框"  # i18n: user-facing

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
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data={"accept": accept}, summary="对话框已处理")
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 处理对话框失败")


@register_node
class CDPUploadFileNode(_CDPNodeBase):
    name = "cdp_upload_file"
    display_name = "CDP 上传文件"  # i18n: user-facing
    description = "通过 CSS 选择器定位 file input 并上传本地文件"  # i18n: user-facing
    icon = "📤"
    default_label = "CDP 上传文件"  # i18n: user-facing

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
            return NodeResult(status=NodeStatus.FAILED, error="未指定 selector 或 file_path")  # i18n: user-facing
        client = self._get_client(inputs, params)
        try:
            await client.connect()
            ok = await client.upload_file(selector, file_path)
            await client.disconnect()
            if ok:
                return NodeResult(
                    # i18n: user-facing
                    status=NodeStatus.SUCCESS,
                    data={"uploaded": file_path},
                    summary=f"已上传: {file_path}",  # i18n: user-facing
                )
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error="上传失败", summary="CDP 上传失败")
        except Exception as e:
            await client.disconnect()
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 上传失败")  # i18n: user-facing


@register_node
class CDPHeapSnapshotNode(_CDPNodeBase):
    name = "cdp_heapsnapshot"
    display_name = "CDP 堆快照"  # i18n: user-facing
    description = "采集 V8 堆快照 (HeapProfiler)"  # i18n: user-facing
    icon = "🧠"
    default_label = "CDP 堆快照"  # i18n: user-facing

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
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data={"snapshot_size": len(snapshot)}, summary="堆快照已采集")
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 堆快照失败")


@register_node
class CDPPerformanceTraceNode(_CDPNodeBase):
    name = "cdp_performance_trace"
    display_name = "CDP 性能追踪"  # i18n: user-facing
    description = "启动性能追踪, 执行动作后获取性能指标 (Tracing + Performance)"  # i18n: user-facing
    icon = "📊"
    default_label = "CDP 性能追踪"  # i18n: user-facing

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
            # i18n: user-facing
            return NodeResult(status=NodeStatus.SUCCESS, data={"traced": True}, summary="性能追踪完成")
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP 性能追踪失败")


@register_node
class CDPLighthouseNode(_CDPNodeBase):
    name = "cdp_lighthouse"
    display_name = "CDP Lighthouse 审计"  # i18n: user-facing
    # i18n: user-facing
    description = "采集页面性能指标 (Lighthouse 替代; 需独立 lighthouse 进程做完整审计)"
    icon = "🚦"
    default_label = "CDP Lighthouse 审计"  # i18n: user-facing

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
                summary=f"性能指标: {len(metrics.get('metrics', []))} 项",  # i18n: user-facing
            )
        except Exception as e:
            await client.disconnect()
            # i18n: user-facing
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="CDP Lighthouse 失败")
