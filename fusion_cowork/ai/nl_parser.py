"""Natural language -> workflow parser.

The user types one sentence (e.g. "sort all PDFs on my desktop by topic"),
the AI parses it and generates a complete workflow definition.

Calls the fusion-mlx /v1/chat/completions endpoint for LLM inference.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from ..engine.kernel_gates import JSONRepairError, parse_llm_json
from .mlx_client import FusionMLXClient

logger = logging.getLogger(__name__)


class WorkflowParseError(Exception):
    """LLM output could not be parsed as workflow JSON (raw_output kept for retry with feedback)."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


def extract_json_object(content: str) -> str:
    """Extract the first complete JSON object from LLM output as text.

    Handles three common shapes: ```json fences, leading prose, trailing prose.
    Uses string-aware brace balancing (ignores braces and escapes inside string
    literals), more robust than a naive split("```").
    """
    import re

    text = content.strip()
    # 1) Prefer the markdown fenced block first (when present, its interior is the JSON)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 2) Locate the first '{', then do string-aware brace balancing
    start = text.find("{")
    if start == -1:
        raise WorkflowParseError("no JSON object found in model output", raw_output=content)
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise WorkflowParseError("unbalanced JSON object in model output", raw_output=content)


# System prompt — guides the LLM to convert natural language into workflow definitions.
# i18n: user-facing
SYSTEM_PROMPT = (
    "你是一个 macOS 桌面自动化工作流生成器。将用户的自然语言需求转换为结构化工作流定义。\n\n"  # i18n: user-facing
    "可用的节点类型:\n"  # i18n: user-facing
    "1. desktop_clean - 桌面清理（清理桌面文件，按类型/日期分类）\n"  # i18n: user-facing
    "2. download_organizer - 下载文件夹整理（归档、去重、整理）\n"  # i18n: user-facing
    "3. file_classifier - AI 文件分类（根据内容语义分类）\n"  # i18n: user-facing
    "4. file_batch_rename - 批量重命名（AI 理解内容生成规范名称）\n"  # i18n: user-facing
    "5. ai_summarize - AI 文档摘要（批量总结文档内容）\n"  # i18n: user-facing
    "6. disk_cleaner - 磁盘清理（扫描并清理垃圾文件）\n"  # i18n: user-facing
    "7. file_watcher - 文件监听（监听目录变化触发流程）\n"  # i18n: user-facing
    "8. file_copy - 文件复制/备份\n"  # i18n: user-facing
    "9. file_move - 文件移动\n"  # i18n: user-facing
    "10. file_delete - 文件删除\n"  # i18n: user-facing
    "11. filter - 条件过滤（按条件筛选文件）\n"  # i18n: user-facing
    "12. loop - 循环处理（批量处理每个文件）\n"  # i18n: user-facing
    "13. file_input - 文件输入（读取文件列表）\n"  # i18n: user-facing
    "14. file_output - 文件输出（写入结果到文件）\n\n"  # i18n: user-facing
    "返回格式: 严格的 JSON 格式，包含 nodes 和 edges 数组。\n"  # i18n: user-facing
    "每个节点包含: id, name, config.params (节点参数)\n\n"  # i18n: user-facing
    "示例:\n"  # i18n: user-facing
    '用户输入: "每天晚上9点自动清理下载文件夹并备份"\n'  # i18n: user-facing
    "输出:\n"  # i18n: user-facing
    "{\n"
    '  "name": "下载文件夹定时清理备份",\n'  # i18n: user-facing
    '  "description": "每天晚上9点自动清理下载文件夹并备份到文稿",\n'  # i18n: user-facing
    '  "nodes": [\n'
    '    {"id": "n1", "name": "file_input", "config": {"params": {"path": "~/Downloads", "recursive": false}}},\n'
    '    {"id": "n2", "name": "download_organizer", "config": {"params": {"action": "clean", "days_threshold": 30}}},\n'
    '    {"id": "n3", "name": "file_copy", "config": {"params": {"destination": "~/Documents/Downloads_Backup", "create_subdir": true}}}\n'
    "  ],\n"
    '  "edges": [\n'
    '    {"source_id": "n1", "target_id": "n2"},\n'
    '    {"source_id": "n2", "target_id": "n3"}\n'
    "  ]\n"
    "}\n\n"
    "路径规则: 只能使用用户在需求里明确给出的路径。用户没有给出路径时, 对应参数\n"  # i18n: user-facing
    '填空字符串 "" 留待用户在参数面板填写, 严禁编造任何不存在的文件/目录路径。\n\n'  # i18n: user-facing
    "只返回 JSON，不要其他解释。"
)


# #85: mlx /v1/models does not guarantee chat models come first (image/video models
# like FLUX/Wan may be listed first). Non-chat engine_type set; both metadata.engine_type
# and top-level engine_type are checked.
# #131: draft = speculative-decoding draft model, must not be picked as chat default.
_NON_CHAT_ENGINE_TYPES = {
    "image_gen",
    "video_gen",
    "image",
    "video",
    "tts",
    "stt",
    "audio",
    "embedding",
    "rerank",
    "draft",
}


def _is_chat_model(m: Dict[str, Any]) -> bool:
    """#126: single-model chat capability check — engine_type not in the non-chat
    set means chat-capable.

    #131: when the gateway (fusion-gateway#245) attaches capabilities metadata on
    /v1/models, capabilities.embedding=True or text_generation=False immediately
    means non-chat (also correct for sources lacking engine_type and carrying only
    modality, e.g. bge-m3 modality=text).
    """
    if not isinstance(m, dict):
        return False
    mid = m.get("id") or m.get("model")
    if not mid:
        return False
    meta = m.get("metadata") or {}
    caps = m.get("capabilities") or meta.get("capabilities") or {}
    if isinstance(caps, dict) and caps:
        if caps.get("embedding") is True:
            return False
        text_gen = caps.get("text_generation")
        if isinstance(text_gen, bool):
            return text_gen
    engine = (m.get("engine_type") or meta.get("engine_type") or "").strip().lower()
    return not (engine and engine in _NON_CHAT_ENGINE_TYPES)


def _pick_chat_model(models: List[Dict[str, Any]]) -> str:
    """Pick a chat model id from list_models() results.

    Skips image_gen/video_gen/tts/embedding engine_types;
    prefers Qwen/Llama instruct; otherwise takes the first chat model.
    """
    chat_candidates: List[str] = []
    for m in models or []:
        if not _is_chat_model(m):
            continue
        chat_candidates.append(m.get("id") or m.get("model"))
    if not chat_candidates:
        return ""
    # Prefer instruct/chat/qwen/llama naming
    prefer_keywords = ("instruct", "chat", "qwen", "llama", "gemma", "mistral")
    for mid in chat_candidates:
        low = mid.lower()
        if any(k in low for k in prefer_keywords):
            return mid
    return chat_candidates[0]


_PATH_LIKE_RE = re.compile(r"^(?:~|/|\./|\.\./)[^\s]*$")


def _sanitize_generated_paths(workflow_def: Dict[str, Any], user_input: str, context: Optional[Dict[str, Any]]) -> None:
    """issue #509: strip LLM-hallucinated file paths (deterministic fallback,
    does not rely on prompt compliance).

    Param values shaped like absolute/home/relative paths (leading ~, /, ./ ../)
    that do not appear in the user input or context are judged hallucinations,
    blanked for the user to fill in, and recorded in workflow_def["warnings"]
    for the frontend preview card.
    """
    import re as _re

    haystack = user_input or ""
    if context:
        try:
            haystack += " " + json.dumps(context, ensure_ascii=False)
        except (TypeError, ValueError):
            pass

    warnings: List[str] = []
    for node in workflow_def.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        params = (node.get("config") or {}).get("params")
        if not isinstance(params, dict):
            continue
        for key, value in list(params.items()):
            if not isinstance(value, str) or not _PATH_LIKE_RE.match(value.strip()):
                continue
            if value.strip() in haystack:
                continue
            # #509: URL/protocol prefixes are not local paths, leave untouched
            if _re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value.strip()):
                continue
            params[key] = ""
            msg = f"节点 '{node.get('id')}' 参数 '{key}' 的路径 '{value}' 未在需求中出现, 已置空待用户填写 (issue #509)"  # i18n: user-facing
            warnings.append(msg)
            logger.warning(
                f"NL generation dropped suspected hallucinated path: node={node.get('id')} param={key} value={value}"
            )
    if warnings:
        existing = workflow_def.get("warnings")
        if isinstance(existing, list):
            existing.extend(warnings)
        else:
            workflow_def["warnings"] = warnings


class NLWorkflowGenerator:
    """Natural language workflow generator.

    Converts the user's natural language description into an executable
    workflow definition. Backed by fusion-mlx for LLM inference.
    """

    def __init__(self, mlx_client: FusionMLXClient, model: str = ""):
        self.mlx = mlx_client
        self.model = model  # empty string means use the default model

    async def generate(
        self,
        user_input: str,
        context: Optional[Dict[str, Any]] = None,
        system_prompt: str = "",
        raise_on_parse_error: bool = False,
    ) -> Dict[str, Any]:
        """Convert natural language into a workflow definition.

        Args:
            user_input: the user's natural language description
            context: optional context info (current directory, file list, etc.)
            system_prompt: optional custom system prompt (plugins/hosts may replace
                the default desktop-automation prompt with their own node catalog;
                empty string uses the default)
            raise_on_parse_error: when True, raise WorkflowParseError on parse
                failure (lets the host retry with feedback); False (default, back
                compatible) returns an empty workflow carrying an error field

        Returns:
            dict: workflow definition with name, description, nodes, edges
        """
        # Resolve the chat model first: explicit user value > FUSION_MLX_MODEL env
        # > filter list_models for a chat model > default.
        # #85: models[0] may be an image_gen/video_gen (FLUX/Wan) that does not support
        # chat completions; blindly taking it yields -32603.
        if not self.model:
            env_model = os.environ.get("FUSION_MLX_MODEL", "").strip()
            if env_model:
                self.model = env_model
                logger.info(f"Using FUSION_MLX_MODEL env model: {self.model}")

        if not self.model:
            try:
                models = await self.mlx.list_models()
                chat_model = _pick_chat_model(models)
                if chat_model:
                    self.model = chat_model
                    logger.info(f"Auto-selected chat model: {self.model}")
                else:
                    logger.warning(f"No chat model found in model list ({len(models)} total), falling back to default")
            except Exception as e:
                logger.warning(f"Failed to fetch model list: {e}, using default model")

        if not self.model:
            self.model = "Qwen3.8-27B-4bit"  # default chat model (#85 fix: old qwen3.5-9b does not exist)

        # Build context info
        context_str = ""
        if context:
            ctx_parts = []
            for key, value in context.items():
                if isinstance(value, (list, dict)):
                    ctx_parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:500]}")
                else:
                    ctx_parts.append(f"{key}: {value}")
            context_str = "\n上下文信息:\n" + "\n".join(ctx_parts)  # i18n: user-facing

        messages = [
            {"role": "system", "content": system_prompt.strip() or SYSTEM_PROMPT},
            {"role": "user", "content": f"{user_input}{context_str}"},
        ]

        # Call fusion-mlx for LLM inference
        response = await self.mlx.chat(
            model=self.model,
            messages=messages,
            temperature=0.2,  # low temperature for stable output
            max_tokens=4096,
        )

        content = response.content.strip()

        # Extract JSON (string-aware brace balancing; handles fences/leading/trailing noise)
        try:
            json_str = extract_json_object(content)
            # issue #554: repair ladder covers fences/trailing commas/truncation — falls back to original path on total failure
            workflow_def = parse_llm_json(json_str, context="nl_generate")

            # Ensure required fields
            workflow_def.setdefault("name", "AI 生成的工作流")  # i18n: user-facing
            workflow_def.setdefault("description", user_input)
            workflow_def.setdefault("nodes", [])
            workflow_def.setdefault("edges", [])

            # CR-13b: validate each node's name against NodeRegistry, drop unknown
            # names (the LLM may hallucinate node names)
            try:
                from ..engine.node import NodeRegistry

                known = {item.get("name", "") for item in NodeRegistry.list()}
                if known:
                    kept = []
                    dropped = []
                    for n in workflow_def["nodes"]:
                        nm = n.get("name", "") if isinstance(n, dict) else ""
                        if nm in known:
                            kept.append(n)
                        else:
                            dropped.append(nm)
                    if dropped:
                        logger.warning(f"NL generation dropped unregistered node names: {dropped}")
                    workflow_def["nodes"] = kept
            except Exception as ve:
                logger.debug(f"NL node name validation skipped (NodeRegistry not ready): {ve}")

            # issue #509: deterministic hallucinated-path removal — path params absent from
            # user input/context are blanked
            try:
                _sanitize_generated_paths(workflow_def, user_input, context)
            except Exception as se:
                logger.warning(f"NL path sanitization skipped (non-blocking): {se}")

            logger.info(f"Workflow generated: {workflow_def['name']} ({len(workflow_def['nodes'])} nodes)")
            return workflow_def

        except (JSONRepairError, WorkflowParseError) as e:
            logger.error(f"Failed to parse LLM output as JSON: {e}")
            logger.debug(f"LLM raw output: {content}")
            if raise_on_parse_error:
                if isinstance(e, WorkflowParseError):
                    raise
                raise WorkflowParseError(f"invalid JSON: {e}", raw_output=content) from e
            return {
                "name": "AI 生成的工作流",  # i18n: user-facing
                "description": user_input,
                "nodes": [],
                "edges": [],
                "error": f"解析失败: {e}",  # i18n: user-facing
                "raw_output": content,
            }

    async def suggest_templates(
        self,
        user_input: str,
    ) -> List[Dict[str, Any]]:
        """Recommend suitable templates based on the user input.

        Args:
            user_input: the user's description

        Returns:
            list[dict]: recommended template list
        """
        messages = [
            {
                "role": "system",
                "content": "你是一个桌面自动化助手。根据用户的需求，推荐最合适的自动化模板。"  # i18n: user-facing
                "返回 JSON 数组，每项包含 name, description, match_score。"  # i18n: user-facing
                "只返回 JSON。",  # i18n: user-facing
            },
            {"role": "user", "content": user_input},
        ]

        response = await self.mlx.chat(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=1024,
        )

        content = response.content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            # issue #554: LLM output JSON repair ladder (kernel json_repair) replaces bare json.loads
            return parse_llm_json(content, context="template_recommend")
        except JSONRepairError:
            logger.warning(f"Template recommendation parse failed: {content[:200]}")
            return []
