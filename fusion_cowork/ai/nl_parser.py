"""Natural language → workflow parser.

The user types one sentence (e.g. "help me auto-classify and archive all PDFs on the desktop by topic"),
and the AI parses it into a complete workflow definition automatically.

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
    """LLM output could not be parsed as workflow JSON (raw_output enables retry with feedback)."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


def extract_json_object(content: str) -> str:
    """Extract the text of the first complete JSON object from LLM output.

    Handles three common shapes: ```json fences, leading prose, trailing commentary.
    Uses string-aware brace balancing (ignores braces and escapes inside string
    literals), more robust than a fragile split("```").
    """
    import re

    text = content.strip()
    # 1) Prefer the markdown fenced block (when present, the fenced content is the JSON)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 2) Locate the first '{' and do string-aware brace balancing
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


# System prompt — guides the LLM to convert natural language into a workflow definition
SYSTEM_PROMPT = """You are a macOS desktop automation workflow generator. Convert the user's natural-language request into a structured workflow definition.

Available node types:
1. desktop_clean - desktop cleanup (clean desktop files, classify by type/date)
2. download_organizer - downloads folder organization (archive, dedupe, tidy)
3. file_classifier - AI file classification (classify by content semantics)
4. file_batch_rename - batch rename (AI understands content and generates standardized names)
5. ai_summarize - AI document summary (batch-summarize document contents)
6. disk_cleaner - disk cleanup (scan and remove junk files)
7. file_watcher - file watching (watch directory changes to trigger flows)
8. file_copy - file copy/backup
9. file_move - file move
10. file_delete - file delete
11. filter - conditional filter (filter files by condition)
12. loop - loop processing (batch-process each file)
13. file_input - file input (read a file list)
14. file_output - file output (write results to a file)

Return format: strict JSON containing nodes and edges arrays.
Each node contains: id, name, config.params (node parameters)

Example:
User input: "Automatically clean the downloads folder every night at 9pm and back it up"
Output:
{
  "name": "Scheduled downloads folder cleanup and backup",
  "description": "Automatically clean the downloads folder every night at 9pm and back it up to Documents",
  "nodes": [
    {"id": "n1", "name": "file_input", "config": {"params": {"path": "~/Downloads", "recursive": false}}},
    {"id": "n2", "name": "download_organizer", "config": {"params": {"action": "clean", "days_threshold": 30}}},
    {"id": "n3", "name": "file_copy", "config": {"params": {"destination": "~/Documents/Downloads_Backup", "create_subdir": true}}}
  ],
  "edges": [
    {"source_id": "n1", "target_id": "n2"},
    {"source_id": "n2", "target_id": "n3"}
  ]
}

Path rules: only use paths the user explicitly gave in the request. When the user gave no path, fill the
corresponding parameter with an empty string "" for the user to complete in the parameter panel; fabricating
any non-existent file/directory path is strictly forbidden.

Return JSON only, no other commentary."""


# #85: mlx /v1/models does not guarantee chat models come first (image/video models like FLUX/Wan may rank first).
# Non-chat engine_type set; judged via metadata.engine_type or top-level engine_type.
# #131: draft = speculative-decoding draft model, must not be the chat default.
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
    """#126: per-model chat capability check — engine_type not in the non-chat set means chat-capable.

    #131: when the gateway (fusion-gateway#245) attaches capabilities metadata on /v1/models,
    capabilities.embedding=True or text_generation=False is judged non-chat directly
    (sources missing engine_type or carrying only modality are handled correctly too, e.g. bge-m3 modality=text).
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

    Skips image_gen/video_gen/tts/embedding engine types;
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
    """issue #509: strip LLM-hallucinated file paths (deterministic backstop, does not rely on prompt compliance).

    Parameter values that look like absolute/home/relative paths (prefixed with ~, /, ./ or ../) and do not
    appear in the user input or context → judged hallucinated, blanked for the user to fill in, and written
    into workflow_def ["warnings"] for the frontend preview card.
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
            # #509: URL/scheme prefixes are not local paths, leave untouched
            if _re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value.strip()):
                continue
            params[key] = ""
            # i18n: user-facing
            msg = f"节点 '{node.get('id')}' 参数 '{key}' 的路径 '{value}' 未在需求中出现, 已置空待用户填写 (issue #509)"
            warnings.append(msg)
            logger.warning(f"NL generation stripped suspected hallucinated path: node={node.get('id')} param={key} value={value}")
    if warnings:
        existing = workflow_def.get("warnings")
        if isinstance(existing, list):
            existing.extend(warnings)
        else:
            workflow_def["warnings"] = warnings


class NLWorkflowGenerator:
    """Natural language workflow generator.

    Converts the user's natural language description into an executable workflow definition.
    Underlying LLM inference goes through fusion-mlx.
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
            system_prompt: optional custom system prompt (plugins/hosts may replace the
                default desktop automation prompt with their own node catalog; empty string uses the default)
            raise_on_parse_error: when True, parse failure raises WorkflowParseError (for host
                retry with feedback); when False (default, backward compatible) returns an empty workflow with error

        Returns:
            dict: workflow definition containing name, description, nodes, edges
        """
        # Determine the chat model first: explicit user value > FUSION_MLX_MODEL env > filter list_models for a chat model > default.
        # #85: models[0] may be image_gen/video_gen (FLUX/Wan) which does not support chat completions; blindly taking it yields -32603.
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
                    logger.warning(f"No chat model found in model list, {len(models)} total, falling back to default")
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
            context_str = "\nContext info:\n" + "\n".join(ctx_parts)

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

        # Extract JSON (string-aware brace balancing, handles fences/leading/trailing noise)
        try:
            json_str = extract_json_object(content)
            # issue #554: repair ladder catches fenced/trailing-comma/truncated shapes — falls back to the original path when all fail
            workflow_def = parse_llm_json(json_str, context="nl_generate")

            # Ensure required fields exist
            # i18n: user-facing
            workflow_def.setdefault("name", "AI 生成的工作流")
            workflow_def.setdefault("description", user_input)
            workflow_def.setdefault("nodes", [])
            workflow_def.setdefault("edges", [])

            # CR-13b: per-node check that name ∈ NodeRegistry, drop unknown names (LLM may hallucinate node names)
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

            # issue #509: deterministic hallucinated-path stripping — path params not present in user input/context are blanked
            try:
                _sanitize_generated_paths(workflow_def, user_input, context)
            except Exception as se:
                logger.warning(f"NL path sanitization skipped (non-blocking): {se}")

            logger.info(f"Workflow generated successfully: {workflow_def['name']} ({len(workflow_def['nodes'])} nodes)")
            return workflow_def

        except (JSONRepairError, WorkflowParseError) as e:
            logger.error(f"Failed to parse LLM output as JSON: {e}")
            logger.debug(f"LLM raw output: {content}")
            if raise_on_parse_error:
                if isinstance(e, WorkflowParseError):
                    raise
                raise WorkflowParseError(f"invalid JSON: {e}", raw_output=content) from e
            return {
                # i18n: user-facing
                "name": "AI 生成的工作流",
                "description": user_input,
                "nodes": [],
                "edges": [],
                # i18n: user-facing
                "error": f"解析失败: {e}",
                "raw_output": content,
            }

    async def suggest_templates(
        self,
        user_input: str,
    ) -> List[Dict[str, Any]]:
        """Recommend suitable templates based on user input.

        Args:
            user_input: the user's description

        Returns:
            list[dict]: list of recommended templates
        """
        messages = [
            {
                "role": "system",
                "content": "You are a desktop automation assistant. Based on the user's request, recommend the most suitable automation templates. "
                "Return a JSON array where each item contains name, description, match_score. "
                "Return JSON only.",
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
