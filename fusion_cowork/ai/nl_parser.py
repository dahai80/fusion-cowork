"""自然语言 → 工作流解析器。

用户输入一句话（如"帮我把桌面所有 PDF 按主题自动分类归档"），
AI 自动解析生成完整的工作流定义。

调用 fusion-mlx 的 /v1/chat/completions 端点进行 LLM 推理。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .mlx_client import FusionMLXClient

logger = logging.getLogger(__name__)

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
    ) -> Dict[str, Any]:
        """将自然语言转换为工作流定义。

        Args:
            user_input: 用户的自然语言描述
            context: 可选的上下文信息（当前目录、文件列表等）

        Returns:
            dict: 工作流定义，包含 name, description, nodes, edges
        """
        # 先获取可用模型列表，如果没有指定模型则使用第一个
        if not self.model:
            try:
                models = await self.mlx.list_models()
                if models:
                    self.model = models[0].get("id", models[0].get("model", ""))
                    logger.info(f"自动选择模型: {self.model}")
            except Exception as e:
                logger.warning(f"获取模型列表失败: {e}，使用默认模型")

        if not self.model:
            self.model = "qwen3.5-9b"  # 默认模型

        # 构建上下文信息
        context_str = ""
        if context:
            ctx_parts = []
            for key, value in context.items():
                if isinstance(value, (list, dict)):
                    import json
                    ctx_parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:500]}")
                else:
                    ctx_parts.append(f"{key}: {value}")
            context_str = "\n上下文信息:\n" + "\n".join(ctx_parts)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
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

        # 提取 JSON（处理 LLM 可能输出的 markdown 代码块）
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()
        else:
            json_str = content

        try:
            workflow_def = json.loads(json_str)

            # 确保必有字段
            workflow_def.setdefault("name", "AI 生成的工作流")
            workflow_def.setdefault("description", user_input)
            workflow_def.setdefault("nodes", [])
            workflow_def.setdefault("edges", [])

            logger.info(f"成功生成工作流: {workflow_def['name']} ({len(workflow_def['nodes'])} 个节点)")
            return workflow_def

        except json.JSONDecodeError as e:
            logger.error(f"解析 LLM 输出为 JSON 失败: {e}")
            logger.debug(f"LLM 原始输出: {content}")
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
                           "只返回 JSON。"
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
