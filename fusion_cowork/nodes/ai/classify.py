"""AI 能力节点 — 调用 fusion-mlx 本地推理引擎实现文件智能处理。

所有 AI 节点通过 HTTP 调用 fusion-mlx 的 /v1/chat/completions 端点，
不直接导入任何 MLX 代码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...engine.node import (
    BaseNode, NodeConfig, NodeResult, NodeStatus,
    NodeCategory, register_node,
)
from ...ai.mlx_client import FusionMLXClient

logger = logging.getLogger(__name__)


@register_node
class AIClassifyNode(BaseNode):
    """AI 文件分类节点 — 根据文件内容语义进行智能分类。

    调用 fusion-mlx 的 chat 接口，分析文件内容后返回分类标签。
    支持按文件类型、主题、项目等多维度分类。
    """
    name = "ai_classify"
    display_name = "AI 文件分类"
    category = NodeCategory.AI_PROCESSING
    description = "根据文件内容语义进行智能分类"
    icon = "🏷️"
    default_label = "AI 分类"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "categories", "label": "分类体系", "type": "list[str]", "optional": True},
    ]
    outputs = [
        {"key": "classified_files", "label": "分类结果", "type": "list[dict]"},
    ]

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None):
        super().__init__(node_id, config)
        self._mlx: Optional[FusionMLXClient] = None

    @property
    def mlx(self) -> FusionMLXClient:
        if self._mlx is None:
            self._mlx = FusionMLXClient()
        return self._mlx

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "fusion-mlx 模型名称",
                    "default": "qwen3.5-9b",
                },
                "classify_by_content": {
                    "type": "boolean",
                    "description": "是否基于内容而不是文件名分类",
                    "default": True,
                },
                "custom_categories": {
                    "type": "string",
                    "description": "自定义分类列表（逗号分隔，留空则 AI 自动判断）",
                    "default": "",
                },
                "max_files_per_batch": {
                    "type": "integer",
                    "description": "每批处理文件数",
                    "default": 10,
                },
            },
            "required": ["model"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        categories = inputs.get("categories", [])
        params = self.config.params

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"classified_files": [], "message": "没有文件需要分类"},
                summary="没有文件需要分类",
            )

        model = params.get("model", "qwen3.5-9b")
        classify_by_content = params.get("classify_by_content", True)
        custom_categories = params.get("custom_categories", "")
        max_per_batch = params.get("max_files_per_batch", 10)

        # 构建分类体系
        category_list = []
        if custom_categories:
            category_list = [c.strip() for c in custom_categories.split(",") if c.strip()]
        elif categories:
            category_list = categories

        # 分批处理文件
        classified_files = []
        batches = [files[i:i + max_per_batch] for i in range(0, len(files), max_per_batch)]

        for batch_idx, batch in enumerate(batches):
            # 构建文件信息
            file_info = []
            for f in batch:
                path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
                info = {"name": path.name, "path": str(path)}
                if classify_by_content and path.is_file() and path.stat().st_size < 100_000:
                    try:
                        info["content_preview"] = path.read_text(encoding="utf-8", errors="ignore")[:2000]
                    except Exception:
                        info["content_preview"] = ""
                file_info.append(info)

            # 构建分类提示
            categories_hint = ""
            if category_list:
                categories_hint = f"\n分类选项: {', '.join(category_list)}"

            system_prompt = (
                "你是一个智能文件分类助手。根据文件名和内容，为每个文件分配合适的分类标签。"
                f"返回 JSON 数组，每项包含 file_name, category, confidence(0-1), reason。{categories_hint}"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请为以下文件分类:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}"},
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=2048,
                )

                result = self._parse_classification_result(response.content, batch)
                classified_files.extend(result)

            except Exception as e:
                logger.error(f"AI 分类批次 {batch_idx} 失败: {e}")
                # 失败时使用文件名扩展名作为兜底分类
                for f in batch:
                    path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
                    ext = path.suffix.lower().lstrip(".") or "unknown"
                    classified_files.append({
                        "file_name": path.name,
                        "file_path": str(path),
                        "category": f"{ext}_files",
                        "confidence": 0.3,
                        "reason": "AI 分类失败，使用扩展名兜底",
                    })

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "classified_files": classified_files,
                "total": len(classified_files),
                "categories": list(set(f["category"] for f in classified_files)),
            },
            summary=f"已分类 {len(classified_files)} 个文件",
        )

    def _parse_classification_result(self, content: str, batch: list) -> list:
        """解析 LLM 返回的分类结果。"""
        # 提取 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            results = json.loads(content)
            if isinstance(results, dict):
                results = results.get("results", results.get("files", [results]))
            if not isinstance(results, list):
                results = [results]
            return results
        except json.JSONDecodeError:
            logger.warning(f"分类结果解析失败，使用文件名兜底: {content[:200]}")
            return [
                {
                    "file_name": Path(f).name if isinstance(f, str) else Path(f.get("path", "")).name,
                    "file_path": f if isinstance(f, str) else f.get("path", ""),
                    "category": "uncategorized",
                    "confidence": 0.3,
                    "reason": "AI 分类结果解析失败",
                }
                for f in batch
            ]


@register_node
class AISummarizeNode(BaseNode):
    """AI 文档摘要节点 — 批量总结文档内容并生成汇总报告。

    调用 fusion-mlx 的 chat 接口进行文档理解与摘要生成。
    """
    name = "ai_summarize"
    display_name = "AI 文档摘要"
    category = NodeCategory.AI_PROCESSING
    description = "批量总结文档内容并生成汇总报告"
    icon = "📝"
    default_label = "AI 摘要"

    inputs = [
        {"key": "files", "label": "文档列表", "type": "list[file]"},
        {"key": "instruction", "label": "摘要指令", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "summaries", "label": "摘要结果", "type": "list[dict]"},
        {"key": "report", "label": "汇总报告", "type": "string"},
    ]

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None):
        super().__init__(node_id, config)
        self._mlx: Optional[FusionMLXClient] = None

    @property
    def mlx(self) -> FusionMLXClient:
        if self._mlx is None:
            self._mlx = FusionMLXClient()
        return self._mlx

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {"type": "string", "default": "qwen3.5-9b"},
                "summary_length": {
                    "type": "string",
                    "enum": ["short", "medium", "detailed"],
                    "default": "medium",
                },
                "extract_keywords": {"type": "boolean", "default": True},
                "extract_conclusions": {"type": "boolean", "default": True},
                "generate_report": {"type": "boolean", "default": True},
                "max_chars_per_file": {"type": "integer", "default": 8000},
            },
            "required": ["model"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        instruction = inputs.get("instruction", "")
        params = self.config.params

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"summaries": [], "report": ""},
                summary="没有文档需要摘要",
            )

        model = params.get("model", "qwen3.5-9b")
        max_chars = params.get("max_chars_per_file", 8000)
        extract_keywords = params.get("extract_keywords", True)
        extract_conclusions = params.get("extract_conclusions", True)

        summaries = []
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue

            # 读取文件内容
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            except Exception as e:
                logger.warning(f"读取文件失败 {path}: {e}")
                summaries.append({
                    "file_name": path.name,
                    "file_path": str(path),
                    "error": str(e),
                })
                continue

            # 构建摘要提示
            instruction_text = f"\n额外要求: {instruction}" if instruction else ""
            system_prompt = (
                "你是一个文档摘要助手。分析文档内容并生成结构化摘要。"
                f"返回 JSON 格式: title, summary, keywords(数组), conclusions(数组){'。' if extract_conclusions else '。'}"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请为文档 '{path.name}' 生成摘要:\n\n{content[:4000]}"},
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=2048,
                )

                parsed = self._parse_summary(response.content, path)
                summaries.append(parsed)

            except Exception as e:
                logger.error(f"摘要生成失败 {path.name}: {e}")
                summaries.append({
                    "file_name": path.name,
                    "file_path": str(path),
                    "summary": f"[摘要生成失败: {e}]",
                    "keywords": [],
                    "conclusions": [],
                })

        # 生成汇总报告
        report = ""
        if params.get("generate_report", True) and summaries:
            report_lines = ["# AI 文档摘要汇总报告\n"]
            for s in summaries:
                report_lines.append(f"## 📄 {s.get('file_name', 'unknown')}")
                report_lines.append(f"📝 {s.get('summary', '无摘要')[:500]}")
                if s.get("keywords"):
                    report_lines.append(f"🏷️ 关键词: {', '.join(s['keywords'][:10])}")
                if s.get("conclusions"):
                    report_lines.append(f"💡 结论: {'; '.join(s['conclusions'][:5])}")
                report_lines.append("")
            report = "\n".join(report_lines)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"summaries": summaries, "report": report},
            summary=f"已完成 {len(summaries)} 个文档摘要",
        )

    def _parse_summary(self, content: str, path: Path) -> dict:
        """解析 LLM 返回的摘要结果。"""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        try:
            result = json.loads(content)
            result.setdefault("file_name", path.name)
            result.setdefault("file_path", str(path))
            return result
        except json.JSONDecodeError:
            return {
                "file_name": path.name,
                "file_path": str(path),
                "summary": content[:500],
                "keywords": [],
                "conclusions": [],
            }


@register_node
class AIGenerateNameNode(BaseNode):
    """AI 批量重命名节点 — 根据文件内容或文件名智能生成规范名称。

    调用 fusion-mlx 的 chat 接口理解文件内容后生成有意义的文件名。
    """
    name = "ai_generate_name"
    display_name = "AI 智能重命名"
    category = NodeCategory.AI_PROCESSING
    description = "根据文件内容智能生成规范名称"
    icon = "✏️"
    default_label = "AI 重命名"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "naming_pattern", "label": "命名规则", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "renamed_files", "label": "重命名结果", "type": "list[dict]"},
    ]

    def __init__(self, node_id: str = "", config: Optional[NodeConfig] = None):
        super().__init__(node_id, config)
        self._mlx: Optional[FusionMLXClient] = None

    @property
    def mlx(self) -> FusionMLXClient:
        if self._mlx is None:
            self._mlx = FusionMLXClient()
        return self._mlx

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {"type": "string", "default": "qwen3.5-9b"},
                "pattern": {
                    "type": "string",
                    "description": "命名模式，如: {date}_{category}_{index}",
                    "default": "{category}_{index}",
                },
                "use_content": {"type": "boolean", "default": True},
                "lowercase": {"type": "boolean", "default": False},
                "replace_spaces": {"type": "boolean", "default": True},
            },
            "required": ["model"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        params = self.config.params

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"renamed_files": []},
                summary="没有文件需要重命名",
            )

        model = params.get("model", "qwen3.5-9b")
        use_content = params.get("use_content", True)
        lowercase = params.get("lowercase", False)
        replace_spaces = params.get("replace_spaces", True)

        # 构建文件信息
        file_info = []
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            info = {"name": path.name, "stem": path.stem, "suffix": path.suffix, "path": str(path)}
            if use_content and path.is_file() and path.stat().st_size < 50000:
                try:
                    info["content_preview"] = path.read_text(encoding="utf-8", errors="ignore")[:1000]
                except Exception:
                    info["content_preview"] = ""
            file_info.append(info)

        system_prompt = (
            "你是一个文件命名助手。根据文件名和内容，为每个文件生成更有意义的新文件名。"
            "返回 JSON 数组，每项包含: file_name(原文件名), new_name(新文件名,不含扩展名), reason\n"
            "新文件名应简洁、规范、有意义，使用小写字母和短横线连接。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请为以下文件生成新名称:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}"},
        ]

        try:
            response = await self.mlx.chat(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=2048,
            )

            renamed = self._parse_rename_result(response.content, files)
        except Exception as e:
            logger.error(f"AI 重命名失败: {e}")
            renamed = self._fallback_rename(files)

        # 应用规则
        for r in renamed:
            new_name = r.get("new_name", "")
            if lowercase:
                new_name = new_name.lower()
            if replace_spaces:
                new_name = new_name.replace(" ", "_").replace("  ", "_")
            # 保留原始扩展名
            orig_path = Path(r.get("file_path", r.get("file_name", "")))
            r["new_name_with_ext"] = f"{new_name}{orig_path.suffix}"
            r["new_name"] = new_name

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"renamed_files": renamed},
            summary=f"已为 {len(renamed)} 个文件生成新名称",
        )

    def _parse_rename_result(self, content: str, files: list) -> list:
        """解析 LLM 返回的重命名结果。"""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        try:
            results = json.loads(content)
            if isinstance(results, dict):
                results = results.get("results", results.get("files", [results]))
            if not isinstance(results, list):
                results = [results]
            return results
        except json.JSONDecodeError:
            return self._fallback_rename(files)

    def _fallback_rename(self, files: list) -> list:
        """兜底重命名方案。"""
        results = []
        for i, f in enumerate(files):
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            ext = path.suffix.lower()
            results.append({
                "file_name": path.name,
                "file_path": str(path),
                "new_name": f"file_{i+1:03d}",
                "reason": "AI 重命名解析失败，使用序号兜底",
            })
        return results