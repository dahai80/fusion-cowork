"""AI capability nodes — file intelligence via the fusion-mlx local inference engine.

All AI nodes call fusion-mlx's /v1/chat/completions endpoint over HTTP;
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
    """AI file classification node — classifies files by content semantics.

    Calls fusion-mlx's chat endpoint and returns classification labels after analyzing file content.
    Supports multi-dimension classification: file type, topic, project, and more.
    """

    name = "ai_classify"
    # i18n: user-facing
    display_name = "AI 文件分类"
    category = NodeCategory.AI_PROCESSING
    # i18n: user-facing
    description = "根据文件内容语义进行智能分类"
    icon = "🏷️"
    # i18n: user-facing
    default_label = "AI 分类"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "categories", "label": "分类体系", "type": "list[str]", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
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
                    # i18n: user-facing
                    "description": "fusion-mlx 模型名称",
                    "default": "qwen3.5-9b",
                },
                "classify_by_content": {
                    "type": "boolean",
                    # i18n: user-facing
                    "description": "是否基于内容而不是文件名分类",
                    "default": True,
                },
                "custom_categories": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "自定义分类列表（逗号分隔，留空则 AI 自动判断）",
                    "default": "",
                },
                "max_files_per_batch": {
                    "type": "integer",
                    # i18n: user-facing
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
                data={"classified_files": [], "message": "No files to classify"},
                summary="No files to classify",
            )

        model = params.get("model", "qwen3.5-9b")
        classify_by_content = params.get("classify_by_content", True)
        custom_categories = params.get("custom_categories", "")
        max_per_batch = params.get("max_files_per_batch", 10)

        # Build the category taxonomy
        category_list = []
        if custom_categories:
            category_list = [c.strip() for c in custom_categories.split(",") if c.strip()]
        elif categories:
            category_list = categories

        # Process files in batches
        classified_files = []
        # issue #143: aggregate token usage across batches, surfaced in output usage for the run waterfall
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

            # Build the classification hint
            categories_hint = ""
            if category_list:
                categories_hint = f"\nCategory options: {', '.join(category_list)}"

            system_prompt = (
                "You are an intelligent file classification assistant. Based on file names and content, assign each file an appropriate category label."
                f"Return a JSON array where each item contains file_name, category, confidence(0-1), reason. {categories_hint}"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Please classify the following files:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}",
                },
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=2048,
                )

                # issue #143: accumulate this batch's LLM token usage
                usage = getattr(response, "usage", None) or {}
                usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)

                result = self._parse_classification_result(response.content, batch)
                classified_files.extend(result)

            except Exception as e:
                logger.error(f"AI classification batch {batch_idx} failed: {e}")
                # On failure, fall back to the filename extension as the category
                for f in batch:
                    path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
                    ext = path.suffix.lower().lstrip(".") or "unknown"
                    classified_files.append(
                        {
                            "file_name": path.name,
                            "file_path": str(path),
                            "category": f"{ext}_files",
                            "confidence": 0.3,
                            # i18n: user-facing
                            "reason": "AI 分类失败，使用扩展名兜底",
                        }
                    )

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "classified_files": classified_files,
                "total": len(classified_files),
                "categories": list(set(f["category"] for f in classified_files)),
                "usage": dict(usage_totals),
                # issue #150: structured references — classification results traceable to source files
                "citations": [
                    {"type": "file", "path": f.get("file_path", ""), "name": f.get("file_name", "")}
                    for f in classified_files
                    if f.get("file_path")
                ],
            },
            summary=f"Classified {len(classified_files)} file(s)",
        )

    def _parse_classification_result(self, content: str, batch: list) -> list:
        """Parse the classification result returned by the LLM."""
        # Extract the JSON
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
            logger.warning(f"Classification result parse failed, falling back to the filename: {content[:200]}")
            return [
                {
                    "file_name": Path(f).name if isinstance(f, str) else Path(f.get("path", "")).name,
                    "file_path": f if isinstance(f, str) else f.get("path", ""),
                    "category": "uncategorized",
                    "confidence": 0.3,
                    # i18n: user-facing
                    "reason": "AI 分类结果解析失败",
                }
                for f in batch
            ]


@register_node
class AISummarizeNode(BaseNode):
    """AI document summarization node — batch-summarizes document content and generates a consolidated report.

    Calls fusion-mlx's chat endpoint for document understanding and summary generation.
    """

    name = "ai_summarize"
    # i18n: user-facing
    display_name = "AI 文档摘要"
    category = NodeCategory.AI_PROCESSING
    # i18n: user-facing
    description = "批量总结文档内容并生成汇总报告"
    icon = "📝"
    # i18n: user-facing
    default_label = "AI 摘要"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文档列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "instruction", "label": "摘要指令", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "summaries", "label": "摘要结果", "type": "list[dict]"},
        # i18n: user-facing
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
                summary="No documents to summarize",
            )

        model = params.get("model", "qwen3.5-9b")
        max_chars = params.get("max_chars_per_file", 8000)
        _extract_keywords = params.get("extract_keywords", True)
        extract_conclusions = params.get("extract_conclusions", True)

        summaries = []
        # issue #143: aggregate token usage across files, surfaced in output usage for the run waterfall
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue

            # Read the file content
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

            # Build the summarization prompt
            _instruction_text = f"\nAdditional requirements: {instruction}" if instruction else ""
            system_prompt = (
                "You are a document summarization assistant. Analyze the document content and generate a structured summary."
                f"Return JSON format: title, summary, keywords(array), conclusions(array){'.' if extract_conclusions else '.'}"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Please summarize the document '{path.name}':\n\n{content[:4000]}"},
            ]

            try:
                response = await self.mlx.chat(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=2048,
                )

                # issue #143: accumulate this file's LLM token usage
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
                        # i18n: user-facing
                        "summary": f"[摘要生成失败: {e}]",
                        "keywords": [],
                        "conclusions": [],
                    }
                )

        # Generate the consolidated report
        report = ""
        if params.get("generate_report", True) and summaries:
            # i18n: user-facing
            report_lines = ["# AI 文档摘要汇总报告\n"]
            for s in summaries:
                report_lines.append(f"## 📄 {s.get('file_name', 'unknown')}")
                # i18n: user-facing
                report_lines.append(f"📝 {s.get('summary', '无摘要')[:500]}")
                if s.get("keywords"):
                    # i18n: user-facing
                    report_lines.append(f"🏷️ 关键词: {', '.join(s['keywords'][:10])}")
                if s.get("conclusions"):
                    # i18n: user-facing
                    report_lines.append(f"💡 结论: {'; '.join(s['conclusions'][:5])}")
                report_lines.append("")
            report = "\n".join(report_lines)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "summaries": summaries,
                "report": report,
                "usage": dict(usage_totals),
                # issue #150: structured references — summary report traceable to source files
                "citations": [
                    {"type": "file", "path": s.get("file_path", ""), "name": s.get("file_name", "")}
                    for s in summaries
                    if s.get("file_path")
                ],
            },
            summary=f"Summarized {len(summaries)} document(s)",
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
    """AI batch rename node — intelligently generates standardized names from file content or file names.

    Calls fusion-mlx's chat endpoint to understand file content, then generates meaningful file names.
    """

    name = "ai_generate_name"
    # i18n: user-facing
    display_name = "AI 智能重命名"
    category = NodeCategory.AI_PROCESSING
    # i18n: user-facing
    description = "根据文件内容智能生成规范名称"
    icon = "✏️"
    # i18n: user-facing
    default_label = "AI 重命名"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "naming_pattern", "label": "命名规则", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
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
                    # i18n: user-facing
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
                summary="No files to rename",
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
            "You are a file naming assistant. Based on file names and content, generate a more meaningful new file name for each file."
            "Return a JSON array where each item contains: file_name (original name), new_name (new name, without extension), reason\n"
            "New file names should be concise, standardized and meaningful, using lowercase letters joined with hyphens."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Please generate new names for the following files:\n{json.dumps(file_info, ensure_ascii=False, indent=2)}",
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

        # Apply the rules
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
            summary=f"Generated new names for {len(renamed)} file(s)",
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
        """Fallback rename strategy."""
        results = []
        for i, f in enumerate(files):
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            _ext = path.suffix.lower()
            results.append(
                {
                    "file_name": path.name,
                    "file_path": str(path),
                    "new_name": f"file_{i + 1:03d}",
                    # i18n: user-facing
                    "reason": "AI 重命名解析失败，使用序号兜底",
                }
            )
        return results
