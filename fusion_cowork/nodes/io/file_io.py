"""IO 节点 — 文件输入/输出操作。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from ...engine.node import (
    BaseNode, NodeResult, NodeStatus,
    NodeCategory, register_node,
)

logger = logging.getLogger(__name__)


@register_node
class FileInputNode(BaseNode):
    """文件输入节点 — 读取文件列表或目录内容作为工作流输入。"""
    name = "file_input"
    display_name = "文件输入"
    category = NodeCategory.IO
    description = "读取文件列表或目录内容作为工作流数据源"
    icon = "📂"
    default_label = "文件输入"

    inputs = [
        {"key": "path", "label": "路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "total_count", "label": "文件总数", "type": "integer"},
        {"key": "total_size", "label": "总大小", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "~", "description": "输入路径（文件或目录）"},
                "recursive": {"type": "boolean", "default": True, "description": "递归子目录"},
                "file_patterns": {"type": "string", "default": "*", "description": "文件过滤模式，逗号分隔"},
                "max_files": {"type": "integer", "default": 5000, "description": "最大文件数"},
                "include_hidden": {"type": "boolean", "default": False},
                "sort_by": {
                    "type": "string",
                    "enum": ["name", "date", "size", "type", ""],
                    "default": "name",
                    "description": "排序方式",
                },
                "sort_order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
            },
            "required": ["path"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        input_path = inputs.get("path", params.get("path", ""))

        if not input_path:
            return NodeResult(status=NodeStatus.FAILED, error="未指定输入路径")

        path = Path(input_path).expanduser()
        if not path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"路径不存在: {input_path}")

        recursive = params.get("recursive", True)
        patterns = [p.strip() for p in params.get("file_patterns", "*").split(",") if p.strip()]
        max_files = params.get("max_files", 5000)
        include_hidden = params.get("include_hidden", False)
        sort_by = params.get("sort_by", "name")
        sort_order = params.get("sort_order", "asc")

        import fnmatch
        files = []

        if path.is_file():
            files = [str(path)]
        else:
            for root, dirs, filenames in os.walk(str(path)):
                if not include_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    filenames = [f for f in filenames if not f.startswith(".")]

                for name in filenames:
                    if not patterns or any(fnmatch.fnmatch(name, p) for p in patterns):
                        fpath = Path(root) / name
                        files.append(str(fpath))
                        if len(files) >= max_files:
                            break
                if not recursive:
                    break
                if len(files) >= max_files:
                    break

        # 排序
        # 排序
        reverse = sort_order == "desc"
        if sort_by == "name":
            files.sort(key=lambda f: Path(f).name.lower(), reverse=reverse)
        elif sort_by == "date":
            files.sort(key=lambda f: Path(f).stat().st_mtime, reverse=reverse)
        elif sort_by == "size":
            files.sort(key=lambda f: Path(f).stat().st_size, reverse=reverse)
        elif sort_by == "type":
            files.sort(key=lambda f: Path(f).suffix.lower(), reverse=reverse)

        total_size = sum(Path(f).stat().st_size for f in files if Path(f).is_file())

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "files": files,
                "total_count": len(files),
                "total_size": self._format_size(total_size),
                "total_size_bytes": total_size,
                "source_path": str(path),
            },
            summary=f"读取 {len(files)} 个文件 ({self._format_size(total_size)})",
        )

    def _format_size(self, size_bytes: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f}{unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f}TB"


@register_node
class FileOutputNode(BaseNode):
    """文件输出节点 — 将工作流结果写入文件。"""
    name = "file_output"
    display_name = "文件输出"
    category = NodeCategory.IO
    description = "将工作流处理结果写入文件"
    icon = "💾"
    default_label = "文件输出"

    inputs = [
        {"key": "data", "label": "输出数据", "type": "any"},
        {"key": "output_path", "label": "输出路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "file_path", "label": "输出文件路径", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "default": "~/Desktop/Output", "description": "输出目录"},
                "file_name": {"type": "string", "default": "report_{date}", "description": "输出文件名（不含扩展名）"},
                "format": {"type": "string", "enum": ["json", "text", "markdown", "csv"], "default": "json"},
                "overwrite": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        data = inputs.get("data", {})
        params = self.config.params

        output_path = inputs.get("output_path", params.get("output_path", "~/Desktop/Output"))
        file_name = params.get("file_name", "report_{date}")
        fmt = params.get("format", "json")
        overwrite = params.get("overwrite", False)

        out_dir = Path(output_path).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        import time
        date_str = time.strftime("%Y%m%d_%H%M%S")
        actual_name = file_name.replace("{date}", date_str)

        ext_map = {"json": ".json", "text": ".txt", "markdown": ".md", "csv": ".csv"}
        ext = ext_map.get(fmt, ".json")

        out_path = out_dir / f"{actual_name}{ext}"
        if out_path.exists() and not overwrite:
            out_path = out_dir / f"{actual_name}_{int(time.time())}{ext}"

        try:
            if fmt == "json":
                import json as j
                out_path.write_text(j.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            elif fmt == "markdown":
                content = self._to_markdown(data)
                out_path.write_text(content, encoding="utf-8")
            elif fmt == "csv":
                content = self._to_csv(data)
                out_path.write_text(content, encoding="utf-8")
            else:
                out_path.write_text(str(data), encoding="utf-8")

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"file_path": str(out_path), "format": fmt, "size": out_path.stat().st_size},
                summary=f"已输出到 {out_path.name}",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"写入失败: {e}",
                summary="写入失败",
            )

    def _to_markdown(self, data: Any) -> str:
        if isinstance(data, dict):
            lines = ["# 处理报告\n"]
            for k, v in data.items():
                lines.append(f"## {k}")
                if isinstance(v, (list, dict)):
                    lines.append(f"```json\n{json.dumps(v, ensure_ascii=False, indent=2)}\n```")
                else:
                    lines.append(str(v))
                lines.append("")
            return "\n".join(lines)
        if isinstance(data, list):
            lines = ["# 处理报告\n"]
            for i, item in enumerate(data):
                lines.append(f"## 条目 {i+1}")
                lines.append(f"```json\n{json.dumps(item, ensure_ascii=False, indent=2)}\n```")
                lines.append("")
            return "\n".join(lines)
        return str(data)

    def _to_csv(self, data: Any) -> str:
        if isinstance(data, list) and data:
            import csv, io
            output = io.StringIO()
            if isinstance(data[0], dict):
                writer = csv.DictWriter(output, fieldnames=data[0].keys())
                writer.writeheader()
                writer.writerows(data)
            else:
                for item in data:
                    output.write(f"{item}\n")
            return output.getvalue()
        if isinstance(data, dict):
            return "\n".join(f"{k},{v}" for k, v in data.items())
        return str(data)