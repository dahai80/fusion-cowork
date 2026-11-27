"""AI capability nodes — call the fusion-mlx local inference engine for intelligent file processing.

All AI nodes call the fusion-mlx /v1/chat/completions endpoint over HTTP;
no MLX code is imported directly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ...ai.mlx_client import FusionMLXClient
from ...engine.kernel_gates import JSONRepairError, parse_llm_json
from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeResult,
    NodeStatus,
    register_node,
)

logger = logging.getLogger(__name__)


@register_node
class AIClassifyNode(BaseNode):
    """AI file classification node — intelligently classify files by content semantics.

    Calls the fusion-mlx chat interface, analyzes file content and returns
    classification labels. Supports multi-dimensional classification by file
    type, topic, project, etc.
    """

    name = "ai_classify"
    display_name = "AI 文件分类"  # i18n: user-facing
    category = NodeCategory.AI_PROCESSING
    description = "根据文件内容语义进行智能分类"  # i18n: user-facing
    icon = "🏷️"
    default_label = "AI 分类"  # i18n: user-facing

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},  # i18n: user-facing
        {"key": "categories", "label": "分类体系", "type": "list[str]", "optional": True},  # i18n: user-facing
    ]
    outputs = [
        {"key": "classified_files", "label": "分类结果", "type": "list[dict]"},  # i18n: user-facing
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
                    "description": "fusion-mlx 模型名称",  # i18n: user-facing
                    "default": "qwen3.5-9b",
                },
                "classify_by_content": {
                    "type": "boolean",
                    "description": "是否基于内容而不是文件名分类",  # i18n: user-facing
                    "default": True,
                },
                "custom_categories": {
                    "type": "string",
                    "description": "自定义分类列表（逗号分隔，留空则 AI 自动判断）",  # i18n: user-facing
                    "default": "",
                },
                "max_files_per_batch": {
                    "type": "integer",
                    "description": "每批处理文件数",  # i18n: user-facing
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
                data={"classified_files": [], "message": "没有文件需要分类"},  # i18n: user-facing
                summary="没有文件需要分类",  # i18n: user-facing
            )

        model = params.get("model", "qwen3.5-9b")
        classify_by_content = params.get("classify_by_content", True)
        custom_categories = params.get("custom_categories", "")
        max_per_batch = params.get("max_files_per_batch", 10)

        # Build category list
        category_list = []
        if custom_categories:
            category_list = [c.strip() for c in custom_categories.split(",") if c.strip()]
        elif categories:
            category_list = categories

        # Process files in batches
        classified_files = []
        # issue #143: aggregate token usage across batches into the output usage for the run waterfall
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
        batches = [files[i : i + max_per_batch] for i in range(0, len(files), max_per_batch)]

        for batch_idx, batch in enumerate(batches):
            # Build file info
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

            # Build classification prompt
            categories_hint = ""
            if category_list:
                categories_hint = f"\n分类选项: {', '.join(category_list)}"  # i18n: user-facing

            system_prompt = (
                "你是一个智能文件分类助手。根据文件名和内容，为每个文件分配合适的分类标签。"  # i18n: user-facing
                f"返回 JSON 数组，每项包含 file_name, category, confidence(0-1), reason。{categories_hint}"  # i18n: user-facing
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"请为以下文件分类:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}",  # i18n: user-facing
                },
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=2048,
                )

                # issue #143: accumulate token usage for this batch
                usage = getattr(response, "usage", None) or {}
                usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)

                result = self._parse_classification_result(response.content, batch)
                classified_files.extend(result)

            except Exception as e:
                logger.error(f"AI classify batch {batch_idx} failed: {e}")
                # Fall back to file extension as category on failure
                for f in batch:
                    path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
                    ext = path.suffix.lower().lstrip(".") or "unknown"
                    classified_files.append(
                        {
                            "file_name": path.name,
                            "file_path": str(path),
                            "category": f"{ext}_files",
                            "confidence": 0.3,
                            "reason": "AI 分类失败，使用扩展名兜底",  # i18n: user-facing
                        }
                    )

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "classified_files": classified_files,
                "total": len(classified_files),
                "categories": list(set(f["category"] for f in classified_files)),
                "usage": dict(usage_totals),
                # issue #150: structured citations — classification results traceable to source files
                "citations": [
                    {"type": "file", "path": f.get("file_path", ""), "name": f.get("file_name", "")}
                    for f in classified_files
                    if f.get("file_path")
                ],
            },
            summary=f"已分类 {len(classified_files)} 个文件",  # i18n: user-facing
        )

    def _parse_classification_result(self, content: str, batch: list) -> list:
        """Parse the classification result returned by the LLM."""
        # Extract JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            # issue #554: LLM output JSON repair ladder (kernel json_repair) replaces bare json.loads
            results = parse_llm_json(content, context="file_classify")
            if isinstance(results, dict):
                results = results.get("results", results.get("files", [results]))
            if not isinstance(results, list):
                results = [results]
            return results
        except JSONRepairError:
            logger.warning(f"Failed to parse classification result, falling back to file name: {content[:200]}")
            return [
                {
                    "file_name": Path(f).name if isinstance(f, str) else Path(f.get("path", "")).name,
                    "file_path": f if isinstance(f, str) else f.get("path", ""),
                    "category": "uncategorized",
                    "confidence": 0.3,
                    "reason": "AI 分类结果解析失败",  # i18n: user-facing
                }
                for f in batch
            ]


@register_node
class AISummarizeNode(BaseNode):
    """AI document summarization node — batch-summarize document content and generate a summary report.

    Calls the fusion-mlx chat interface for document understanding and summary generation.
    """

    name = "ai_summarize"
    display_name = "AI 文档摘要"  # i18n: user-facing
    category = NodeCategory.AI_PROCESSING
    description = "批量总结文档内容并生成汇总报告"  # i18n: user-facing
    icon = "📝"
    default_label = "AI 摘要"  # i18n: user-facing

    inputs = [
        {"key": "files", "label": "文档列表", "type": "list[file]"},  # i18n: user-facing
        {"key": "instruction", "label": "摘要指令", "type": "string", "optional": True},  # i18n: user-facing
    ]
    outputs = [
        {"key": "summaries", "label": "摘要结果", "type": "list[dict]"},  # i18n: user-facing
        {"key": "report", "label": "汇总报告", "type": "string"},  # i18n: user-facing
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
                summary="没有文档需要摘要",  # i18n: user-facing
            )

        model = params.get("model", "qwen3.5-9b")
        max_chars = params.get("max_chars_per_file", 8000)
        _extract_keywords = params.get("extract_keywords", True)
        extract_conclusions = params.get("extract_conclusions", True)

        summaries = []
        # issue #143: aggregate token usage across files into the output usage for the run waterfall
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue

            # Read file content
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            except Exception as e:
                logger.warning(f"Failed to read file {path}: {e}")
                summaries.append(
                    {
                        "file_name": path.name,
                        "file_path": str(path),
                        "error": str(e),
                    }
                )
                continue

            # Build summarization prompt
            _instruction_text = f"\n额外要求: {instruction}" if instruction else ""  # i18n: user-facing
            system_prompt = (
                "你是一个文档摘要助手。分析文档内容并生成结构化摘要。"  # i18n: user-facing
                f"返回 JSON 格式: title, summary, keywords(数组), conclusions(数组){'。' if extract_conclusions else '。'}"  # i18n: user-facing
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"请为文档 '{path.name}' 生成摘要:\n\n{content[:4000]}",  # i18n: user-facing
                },  # i18n: user-facing
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=2048,
                )

                # issue #143: accumulate token usage for this file
                usage = getattr(response, "usage", None) or {}
                usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)

                parsed = self._parse_summary(response.content, path)
                summaries.append(parsed)

            except Exception as e:
                logger.error(f"Summary generation failed for {path.name}: {e}")
                summaries.append(
                    {
                        "file_name": path.name,
                        "file_path": str(path),
                        "summary": f"[摘要生成失败: {e}]",  # i18n: user-facing
                        "keywords": [],
                        "conclusions": [],
                    }
                )

        # Generate summary report
        report = ""
        if params.get("generate_report", True) and summaries:
            report_lines = ["# AI 文档摘要汇总报告\n"]  # i18n: user-facing
            for s in summaries:
                report_lines.append(f"## 📄 {s.get('file_name', 'unknown')}")
                report_lines.append(f"📝 {s.get('summary', '无摘要')[:500]}")  # i18n: user-facing
                if s.get("keywords"):
                    report_lines.append(f"🏷️ 关键词: {', '.join(s['keywords'][:10])}")  # i18n: user-facing
                if s.get("conclusions"):
                    report_lines.append(f"💡 结论: {'; '.join(s['conclusions'][:5])}")  # i18n: user-facing
                report_lines.append("")
            report = "\n".join(report_lines)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "summaries": summaries,
                "report": report,
                "usage": dict(usage_totals),
                # issue #150: structured citations — summary report traceable to source files
                "citations": [
                    {"type": "file", "path": s.get("file_path", ""), "name": s.get("file_name", "")}
                    for s in summaries
                    if s.get("file_path")
                ],
            },
            summary=f"已完成 {len(summaries)} 个文档摘要",  # i18n: user-facing
        )

    def _parse_summary(self, content: str, path: Path) -> dict:
        """Parse the summary result returned by the LLM."""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        try:
            # issue #554: LLM output JSON repair ladder (kernel json_repair) replaces bare json.loads
            result = parse_llm_json(content, context="file_summarize")
            result.setdefault("file_name", path.name)
            result.setdefault("file_path", str(path))
            return result
        except JSONRepairError:
            return {
                "file_name": path.name,
                "file_path": str(path),
                "summary": content[:500],
                "keywords": [],
                "conclusions": [],
            }


@register_node
class AIGenerateNameNode(BaseNode):
    """AI batch rename node — intelligently generate standardized names from file content or names.

    Calls the fusion-mlx chat interface to understand file content and generate
    meaningful file names.
    """

    name = "ai_generate_name"
    display_name = "AI 智能重命名"  # i18n: user-facing
    category = NodeCategory.AI_PROCESSING
    description = "根据文件内容智能生成规范名称"  # i18n: user-facing
    icon = "✏️"
    default_label = "AI 重命名"  # i18n: user-facing

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},  # i18n: user-facing
        {"key": "naming_pattern", "label": "命名规则", "type": "string", "optional": True},  # i18n: user-facing
    ]
    outputs = [
        {"key": "renamed_files", "label": "重命名结果", "type": "list[dict]"},  # i18n: user-facing
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
                    "description": "命名模式，如: {date}_{category}_{index}",  # i18n: user-facing
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
                summary="没有文件需要重命名",  # i18n: user-facing
            )

        model = params.get("model", "qwen3.5-9b")
        use_content = params.get("use_content", True)
        lowercase = params.get("lowercase", False)
        replace_spaces = params.get("replace_spaces", True)

        # Build file info
        file_info = []
        # issue #143: token usage aggregation (single LLM call)
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
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
            "你是一个文件命名助手。根据文件名和内容，为每个文件生成更有意义的新文件名。"  # i18n: user-facing
            "返回 JSON 数组，每项包含: file_name(原文件名), new_name(新文件名,不含扩展名), reason\n"  # i18n: user-facing
            "新文件名应简洁、规范、有意义，使用小写字母和短横线连接。"  # i18n: user-facing
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"请为以下文件生成新名称:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}",  # i18n: user-facing
            },
        ]

        try:
            response = await self.mlx.chat(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=2048,
            )

            # issue #143: accumulate LLM token usage
            usage = getattr(response, "usage", None) or {}
            usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)

            renamed = self._parse_rename_result(response.content, files)
        except Exception as e:
            logger.error(f"AI rename failed: {e}")
            renamed = self._fallback_rename(files)

        # Apply rules
        for r in renamed:
            new_name = r.get("new_name", "")
            if lowercase:
                new_name = new_name.lower()
            if replace_spaces:
                new_name = new_name.replace(" ", "_").replace("  ", "_")
            # Preserve the original extension
            orig_path = Path(r.get("file_path", r.get("file_name", "")))
            r["new_name_with_ext"] = f"{new_name}{orig_path.suffix}"
            r["new_name"] = new_name

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"renamed_files": renamed, "usage": dict(usage_totals)},
            summary=f"已为 {len(renamed)} 个文件生成新名称",  # i18n: user-facing
        )

    def _parse_rename_result(self, content: str, files: list) -> list:
        """Parse the rename result returned by the LLM."""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        try:
            # issue #554: LLM output JSON repair ladder (kernel json_repair) replaces bare json.loads
            results = parse_llm_json(content, context="file_rename")
            if isinstance(results, dict):
                results = results.get("results", results.get("files", [results]))
            if not isinstance(results, list):
                results = [results]
            return results
        except JSONRepairError:
            return self._fallback_rename(files)

    def _fallback_rename(self, files: list) -> list:
        """Fallback rename scheme."""
        results = []
        for i, f in enumerate(files):
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            _ext = path.suffix.lower()
            results.append(
                {
                    "file_name": path.name,
                    "file_path": str(path),
                    "new_name": f"file_{i + 1:03d}",
                    "reason": "AI 重命名解析失败，使用序号兜底",  # i18n: user-facing
                }
            )
        return results
