"""自然语言 → 工作流解析器。

用户输入一句话（如"帮我把桌面所有 PDF 按主题自动分类归档"），
AI 自动解析生成完整的工作流定义。

调用 fusion-mlx 的 /v1/chat/completions 端点进行 LLM 推理。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .mlx_client import FusionMLXClient

logger = logging.getLogger(__name__)


class WorkflowParseError(Exception):
    """LLM 输出无法解析为工作流 JSON（raw_output 供带反馈的重试）。"""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


def extract_json_object(content: str) -> str:
    """从 LLM 输出中提取第一个完整 JSON 对象的文本。

    处理三种常见形态: ```json 围栏、前置说明文字、尾随解释文字。
    采用字符串感知的花括号配平（忽略字符串字面量内的花括号与转义），
    比脆弱的 split("```") 鲁棒。
    """
    import re

    text = content.strip()
    # 1) 优先取 markdown 围栏块（存在时围栏内就是 JSON）
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 2) 定位第一个 '{'，做字符串感知的括号配平
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


# 系统提示词 — 指导 LLM 将自然语言转换为工作流定义
SYSTEM_PROMPT = """你是一个 macOS 桌面自动化工作流生成器。将用户的自然语言需求转换为结构化工作流定义。

可用的节点类型:
1. desktop_clean - 桌面清理（清理桌面文件，按类型/日期分类）
2. download_organizer - 下载文件夹整理（归档、去重、整理）
3. file_classifier - AI 文件分类（根据内容语义分类）
4. file_batch_rename - 批量重命名（AI 理解内容生成规范名称）
5. ai_summarize - AI 文档摘要（批量总结文档内容）
6. disk_cleaner - 磁盘清理（扫描并清理垃圾文件）
7. file_watcher - 文件监听（监听目录变化触发流程）
8. file_copy - 文件复制/备份
9. file_move - 文件移动
10. file_delete - 文件删除
11. filter - 条件过滤（按条件筛选文件）
12. loop - 循环处理（批量处理每个文件）
13. file_input - 文件输入（读取文件列表）
14. file_output - 文件输出（写入结果到文件）

返回格式: 严格的 JSON 格式，包含 nodes 和 edges 数组。
每个节点包含: id, name, config.params (节点参数)

示例:
用户输入: "每天晚上9点自动清理下载文件夹并备份"
输出:
{
  "name": "下载文件夹定时清理备份",
  "description": "每天晚上9点自动清理下载文件夹并备份到文稿",
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

只返回 JSON，不要其他解释。"""


# #85: mlx /v1/models 不保证 chat 模型在前 (FLUX/Wan 等 image/video 模型可能排第一)。
# 非 chat engine_type 集合; metadata.engine_type 或顶层 engine_type 均判。
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
}


def _is_chat_model(m: Dict[str, Any]) -> bool:
    """#126: 单模型 chat 能力判定 — engine_type 不在非 chat 集合即视为 chat 可用。"""
    if not isinstance(m, dict):
        return False
    mid = m.get("id") or m.get("model")
    if not mid:
        return False
    meta = m.get("metadata") or {}
    engine = (m.get("engine_type") or meta.get("engine_type") or "").strip().lower()
    return not (engine and engine in _NON_CHAT_ENGINE_TYPES)


def _pick_chat_model(models: List[Dict[str, Any]]) -> str:
    """从 list_models() 结果选一个 chat 模型 id。

    跳过 image_gen/video_gen/tts/embedding 等 engine_type;
    优先 Qwen/Llama instruct; 否则取第一个 chat 模型。
    """
    chat_candidates: List[str] = []
    for m in models or []:
        if not _is_chat_model(m):
            continue
        chat_candidates.append(m.get("id") or m.get("model"))
    if not chat_candidates:
        return ""
    # 优先 instruct/chat/qwen/llama 命名
    prefer_keywords = ("instruct", "chat", "qwen", "llama", "gemma", "mistral")
    for mid in chat_candidates:
        low = mid.lower()
        if any(k in low for k in prefer_keywords):
            return mid
    return chat_candidates[0]


class NLWorkflowGenerator:
    """自然语言工作流生成器。

    将用户的自然语言描述转换为可执行的工作流定义。
    底层调用 fusion-mlx 进行 LLM 推理。
    """

    def __init__(self, mlx_client: FusionMLXClient, model: str = ""):
        self.mlx = mlx_client
        self.model = model  # 空字符串表示使用默认模型

    async def generate(
        self,
        user_input: str,
        context: Optional[Dict[str, Any]] = None,
        system_prompt: str = "",
        raise_on_parse_error: bool = False,
    ) -> Dict[str, Any]:
        """将自然语言转换为工作流定义。

        Args:
            user_input: 用户的自然语言描述
            context: 可选的上下文信息（当前目录、文件列表等）
            system_prompt: 可选的自定义系统提示词（插件/宿主可用自己的节点
                目录替换默认的桌面自动化提示词；空串用默认）
            raise_on_parse_error: True 时解析失败抛 WorkflowParseError（供宿主
                带反馈重试）；False（默认，兼容旧调用方）返回带 error 的空工作流

        Returns:
            dict: 工作流定义，包含 name, description, nodes, edges
        """
        # 先确定 chat 模型: 用户显式 > FUSION_MLX_MODEL env > 过滤 list_models 取 chat 模型 > 默认。
        # #85: models[0] 可能是 image_gen/video_gen (FLUX/Wan) 不支持 chat completions, 盲取会 -32603。
        if not self.model:
            env_model = os.environ.get("FUSION_MLX_MODEL", "").strip()
            if env_model:
                self.model = env_model
                logger.info(f"使用 FUSION_MLX_MODEL env 模型: {self.model}")

        if not self.model:
            try:
                models = await self.mlx.list_models()
                chat_model = _pick_chat_model(models)
                if chat_model:
                    self.model = chat_model
                    logger.info(f"自动选择 chat 模型: {self.model}")
                else:
                    logger.warning(f"模型列表中未找到 chat 模型, 共 {len(models)} 个, 回退默认")
            except Exception as e:
                logger.warning(f"获取模型列表失败: {e}，使用默认模型")

        if not self.model:
            self.model = "Qwen3.8-27B-4bit"  # 默认 chat 模型 (#85 修正: 旧 qwen3.5-9b 不存在)

        # 构建上下文信息
        context_str = ""
        if context:
            ctx_parts = []
            for key, value in context.items():
                if isinstance(value, (list, dict)):
                    ctx_parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:500]}")
                else:
                    ctx_parts.append(f"{key}: {value}")
            context_str = "\n上下文信息:\n" + "\n".join(ctx_parts)

        messages = [
            {"role": "system", "content": system_prompt.strip() or SYSTEM_PROMPT},
            {"role": "user", "content": f"{user_input}{context_str}"},
        ]

        # 调用 fusion-mlx 进行 LLM 推理
        response = await self.mlx.chat(
            model=self.model,
            messages=messages,
            temperature=0.2,  # 低温度确保输出稳定
            max_tokens=4096,
        )

        content = response.content.strip()

        # 提取 JSON（字符串感知的花括号配平，处理围栏/前置/尾随噪声）
        try:
            json_str = extract_json_object(content)
            workflow_def = json.loads(json_str)

            # 确保必有字段
            workflow_def.setdefault("name", "AI 生成的工作流")
            workflow_def.setdefault("description", user_input)
            workflow_def.setdefault("nodes", [])
            workflow_def.setdefault("edges", [])

            # CR-13b: 逐节点校验 name ∈ NodeRegistry, 未知名剔除 (LLM 可能幻觉节点名)
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
                        logger.warning(f"NL 生成剔除未注册节点名: {dropped}")
                    workflow_def["nodes"] = kept
            except Exception as ve:
                logger.debug(f"NL 节点名校验跳过 (NodeRegistry 未就绪): {ve}")

            logger.info(f"成功生成工作流: {workflow_def['name']} ({len(workflow_def['nodes'])} 个节点)")
            return workflow_def

        except (json.JSONDecodeError, WorkflowParseError) as e:
            logger.error(f"解析 LLM 输出为 JSON 失败: {e}")
            logger.debug(f"LLM 原始输出: {content}")
            if raise_on_parse_error:
                if isinstance(e, WorkflowParseError):
                    raise
                raise WorkflowParseError(f"invalid JSON: {e}", raw_output=content) from e
            return {
                "name": "AI 生成的工作流",
                "description": user_input,
                "nodes": [],
                "edges": [],
                "error": f"解析失败: {e}",
                "raw_output": content,
            }

    async def suggest_templates(
        self,
        user_input: str,
    ) -> List[Dict[str, Any]]:
        """根据用户输入推荐合适的模板。

        Args:
            user_input: 用户描述

        Returns:
            list[dict]: 推荐的模板列表
        """
        messages = [
            {
                "role": "system",
                "content": "你是一个桌面自动化助手。根据用户的需求，推荐最合适的自动化模板。"
                "返回 JSON 数组，每项包含 name, description, match_score。"
                "只返回 JSON。",
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
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"模板推荐解析失败: {content[:200]}")
            return []
