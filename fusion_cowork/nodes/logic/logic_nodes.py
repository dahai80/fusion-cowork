"""逻辑节点 — 条件过滤、循环等控制流节点。"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...engine.node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus,
    NodeCategory, register_node,
)

logger = logging.getLogger(__name__)


@register_node
class FilterNode(BaseNode):
    """条件过滤节点 — 按条件筛选数据。

    支持按文件类型、大小、日期、名称等维度过滤。
    """
    name = "filter"
    display_name = "条件过滤"
    category = NodeCategory.LOGIC
    description = "按条件筛选数据"
    icon = "🔍"
    default_label = "条件过滤"

    inputs = [
        {"key": "data", "label": "输入数据", "type": "any"},
        {"key": "filter_criteria", "label": "过滤条件", "type": "dict", "optional": True},
    ]
    outputs = [
        {"key": "passed", "label": "通过的数据", "type": "list"},
        {"key": "filtered", "label": "过滤掉的数据", "type": "list"},
        {"key": "passed_count", "label": "通过数量", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filter_type": {
                    "type": "string",
                    "enum": ["extension", "name_pattern", "min_size", "max_size", "date_after", "date_before", "custom"],
                    "default": "extension",
                },
                "filter_value": {"type": "string", "default": ".pdf,.docx,.md", "description": "过滤值"},
                "invert": {"type": "boolean", "default": False, "description": "反向过滤"},
                "case_sensitive": {"type": "boolean", "default": False},
                "data_key": {"type": "string", "default": "files", "description": "输入数据中要过滤的字段名"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        data = inputs.get("data", inputs)
        params = self.config.params

        filter_type = params.get("filter_type", "extension")
        filter_value = params.get("filter_value", "")
        invert = params.get("invert", False)
        case_sensitive = params.get("case_sensitive", False)
        data_key = params.get("data_key", "files")

        # 获取要过滤的数据列表
        items = data
        if isinstance(data, dict) and data_key in data:
            items = data[data_key]
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            items = [items]

        passed = []
        filtered = []

        for item in items:
            item_str = str(item) if not isinstance(item, dict) else item.get("name", item.get("file_name", str(item.get("path", ""))))
            item_path = Path(item_str) if item_str else Path()

            match = self._matches_filter(item, item_str, item_path, filter_type, filter_value, case_sensitive)

            if invert:
                match = not match

            if match:
                passed.append(item)
            else:
                filtered.append(item)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "passed": passed,
                "filtered": filtered,
                "passed_count": len(passed),
                "filtered_count": len(filtered),
            },
            summary=f"通过 {len(passed)}/{len(passed) + len(filtered)} 项",
        )

    def _matches_filter(self, item: Any, item_str: str, item_path: Path,
                        filter_type: str, filter_value: str, case_sensitive: bool) -> bool:
        """检查单个项是否匹配过滤条件。"""
        if not filter_value:
            return True

        if not case_sensitive:
            item_str = item_str.lower()
            filter_value = filter_value.lower()

        if filter_type == "extension":
            exts = [e.strip() for e in filter_value.split(",") if e.strip()]
            if not exts:
                return True
            return item_path.suffix.lower() in [e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts]

        elif filter_type == "name_pattern":
            patterns = [p.strip() for p in filter_value.split(",") if p.strip()]
            return any(fnmatch.fnmatch(item_path.name, p) for p in patterns)

        elif filter_type == "min_size":
            try:
                min_size = int(filter_value)
                if isinstance(item, dict):
                    size = item.get("size", item.get("total_size", 0))
                else:
                    size = item_path.stat().st_size if item_path.exists() else 0
                return size >= min_size
            except (ValueError, OSError):
                return True

        elif filter_type == "max_size":
            try:
                max_size = int(filter_value)
                if isinstance(item, dict):
                    size = item.get("size", item.get("total_size", 0))
                else:
                    size = item_path.stat().st_size if item_path.exists() else 0
                return size <= max_size
            except (ValueError, OSError):
                return True

        elif filter_type == "custom":
            # 简单字符串包含匹配
            return filter_value in item_str

        return True


@register_node
class LoopNode(BaseNode):
    """循环节点 — 对列表中的每个元素执行相同的处理逻辑。

    注意：LoopNode 在 V0.1 中作为单次批量处理，不支持真正的循环展开。
    """
    name = "loop"
    display_name = "循环处理"
    category = NodeCategory.LOGIC
    description = "批量处理列表中的每个元素"
    icon = "🔄"
    default_label = "循环处理"

    inputs = [
        {"key": "items", "label": "待处理列表", "type": "list"},
        {"key": "operation", "label": "处理操作", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "results", "label": "处理结果", "type": "list"},
        {"key": "total", "label": "总数", "type": "integer"},
        {"key": "success_count", "label": "成功数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "batch_size": {"type": "integer", "default": 50, "description": "每批处理数量"},
                "max_items": {"type": "integer", "default": 1000, "description": "最大处理数量"},
                "operation": {
                    "type": "string",
                    "enum": ["passthrough", "collect_metadata", "extract_field"],
                    "default": "passthrough",
                },
                "extract_field": {"type": "string", "default": "name", "description": "要提取的字段名"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        items = inputs.get("items", [])
        params = self.config.params

        if not isinstance(items, list):
            items = list(items) if hasattr(items, '__iter__') else [items]

        batch_size = params.get("batch_size", 50)
        max_items = params.get("max_items", 1000)
        operation = params.get("operation", "passthrough")
        extract_field = params.get("extract_field", "name")

        items = items[:max_items]

        results = []
        success_count = 0

        for item in items:
            try:
                if operation == "passthrough":
                    results.append(item)
                    success_count += 1
                elif operation == "collect_metadata":
                    p = Path(item) if isinstance(item, str) else Path(item.get("path", str(item)))
                    if p.exists():
                        results.append({
                            "name": p.name,
                            "path": str(p),
                            "size": p.stat().st_size if p.is_file() else 0,
                            "modified": p.stat().st_mtime,
                            "is_dir": p.is_dir(),
                        })
                        success_count += 1
                elif operation == "extract_field":
                    if isinstance(item, dict):
                        results.append(item.get(extract_field, item))
                    else:
                        results.append(item)
                    success_count += 1
                else:
                    results.append(item)
                    success_count += 1
            except Exception as e:
                logger.warning(f"循环处理项失败: {e}")
                results.append({"error": str(e), "item": str(item)})

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "results": results,
                "total": len(items),
                "success_count": success_count,
                "failed_count": len(items) - success_count,
            },
            summary=f"处理完成: {success_count}/{len(items)} 成功",
        )


@register_node
class MergeNode(BaseNode):
    """数据合并节点 — 合并多个上游节点的数据。"""
    name = "merge"
    display_name = "数据合并"
    category = NodeCategory.LOGIC
    description = "合并多个来源的数据"
    icon = "🔀"
    default_label = "数据合并"

    inputs = [
        {"key": "data_1", "label": "数据源 1", "type": "any"},
        {"key": "data_2", "label": "数据源 2", "type": "any"},
    ]
    outputs = [
        {"key": "merged", "label": "合并结果", "type": "any"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "merge_mode": {
                    "type": "string",
                    "enum": ["concat", "union", "merge_dict", "merge_deep"],
                    "default": "concat",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        data_1 = inputs.get("data_1", inputs.get("data_1", []))
        data_2 = inputs.get("data_2", inputs.get("data_2", []))
        merge_mode = self.config.params.get("merge_mode", "concat")

        if merge_mode == "concat":
            if isinstance(data_1, list) and isinstance(data_2, list):
                merged = data_1 + data_2
            else:
                merged = [data_1, data_2]
        elif merge_mode == "union":
            if isinstance(data_1, list) and isinstance(data_2, list):
                seen = set()
                merged = []
                for item in data_1 + data_2:
                    key = str(item)
                    if key not in seen:
                        seen.add(key)
                        merged.append(item)
            else:
                merged = [data_1, data_2]
        elif merge_mode == "merge_dict":
            d1 = data_1 if isinstance(data_1, dict) else {}
            d2 = data_2 if isinstance(data_2, dict) else {}
            merged = {**d1, **d2}
        else:
            merged = [data_1, data_2]

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"merged": merged},
            summary="数据合并完成",
        )