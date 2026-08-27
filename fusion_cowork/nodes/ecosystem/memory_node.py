"""Fusion 生态互通节点 — 对接 fusion-memory 长期记忆中枢。

通过 fm-server HTTP JSON-RPC (127.0.0.1, Bearer 鉴权) 写入 / 检索跨 session
记忆。离线硬约束: 仅 HTTP 到本机 fusion-memory, 不连云 (对齐 fusion-memory
100% offline 约束)。

参考: fusion-memory clients/python/fusion_memory_client.py (HTTP wire 契约)
+ clients/README.md (协议矩阵 + env 表)。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import httpx

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:11435"


def _get_base_url() -> str:
    return os.environ.get("FUSION_MEMORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _get_api_key() -> str:
    key = os.environ.get("FUSION_MEMORY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FUSION_MEMORY_API_KEY 未配置 (fm-server Bearer 鉴权, B5)")
    return key


async def _rpc(client: httpx.AsyncClient, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    resp = await client.post(
        f"/v1/memory/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        err = data["error"]
        raise RuntimeError(f"fusion-memory {method} RPC {err['code']}: {err['message']}")
    return data.get("result")


@register_node
class MemoryCommitNode(BaseNode):
    """记忆提交节点 — 把工作流轨迹 (TrajectoryRecorder 输出 / SharedContext)
    作为 Interaction 写入 fusion-memory, 拿 turn 级 memory_id 列表。

    工作流 DAG 末端接入, 形成 *工作流产出 → 长期记忆 → 下次 session 召回*
    的跨 session 闭环。
    """

    name = "memory_commit"
    display_name = "记忆提交"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "把工作流轨迹作为 Interaction 提交到 fusion-memory 长期记忆"
    icon = "🧠"
    default_label = "记忆提交"

    inputs = [
        {"key": "interaction", "label": "Interaction 对象", "type": "object"},
        {"key": "session_id", "label": "会话 ID", "type": "string"},
    ]
    outputs = [
        {"key": "memory_ids", "label": "记忆 ID 列表", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "会话 ID (与 interaction.session_id 一致)",
                },
                "interaction": {
                    "type": "object",
                    "description": (
                        "Interaction 对象: {id, session_id, turns:[{turn_idx,"
                        "user_message, assistant_message, tool_calls}], timestamp, metadata}"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "HTTP 超时秒数",
                    "default": 10.0,
                },
            },
            "required": ["session_id", "interaction"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        session_id = inputs.get("session_id", params.get("session_id", ""))
        interaction = inputs.get("interaction", params.get("interaction", {}))
        timeout = params.get("timeout", 10.0) or 10.0

        if not session_id or not interaction:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="缺少必填参数 session_id 或 interaction",
                summary="参数不完整",
            )

        try:
            key = _get_api_key()
        except RuntimeError as e:
            logger.error(f"MemoryCommitNode 鉴权配置缺失: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(e),
                summary="鉴权未配置",
            )

        logger.info(f"MemoryCommitNode commit session={session_id}")
        try:
            async with httpx.AsyncClient(
                base_url=_get_base_url(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=timeout,
            ) as client:
                result = await _rpc(
                    client,
                    "commit",
                    {"session_id": session_id, "interaction": interaction},
                )
            memory_ids: List[str] = result if isinstance(result, list) else []
            logger.info(f"MemoryCommitNode commit ok: {len(memory_ids)} ids")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"memory_ids": memory_ids, "session_id": session_id},
                summary=f"提交 {len(memory_ids)} 条记忆",
            )
        except Exception as e:
            logger.exception("MemoryCommitNode 执行异常")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"记忆提交异常: {e}",
                summary="执行异常",
            )


@register_node
class MemoryRetrieveNode(BaseNode):
    """记忆检索节点 — 按 query 调 retrieve_context, 拿 FormattedContext
    (blocks + total_tokens) 注入下游节点的 SharedContext。

    工作流 DAG 前端接入, 为后续节点提供跨 session 记忆上下文。
    """

    name = "memory_retrieve"
    display_name = "记忆检索"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "从 fusion-memory 检索跨 session 记忆上下文注入下游"
    icon = "🔍"
    default_label = "记忆检索"

    inputs = [
        {"key": "text", "label": "查询文本", "type": "string"},
    ]
    outputs = [
        {"key": "context", "label": "记忆上下文", "type": "object"},
        {"key": "total_tokens", "label": "总 token 数", "type": "integer"},
        {"key": "block_count", "label": "记忆块数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "查询文本 (语义检索)",
                },
                "top_k": {
                    "type": "integer",
                    "description": "召回数",
                    "default": 10,
                },
                "token_budget": {
                    "type": "integer",
                    "description": "token 预算上限",
                    "default": 4096,
                },
                "aggregate": {
                    "type": "boolean",
                    "description": "是否按 interaction 聚合 turns",
                    "default": True,
                },
                "timeout": {
                    "type": "number",
                    "description": "HTTP 超时秒数",
                    "default": 10.0,
                },
            },
            "required": ["text"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        text = inputs.get("text", params.get("text", ""))
        top_k = params.get("top_k", 10)
        token_budget = params.get("token_budget", 4096)
        aggregate = params.get("aggregate", True)
        timeout = params.get("timeout", 10.0) or 10.0

        if not text:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="缺少必填参数 text",
                summary="参数不完整",
            )

        try:
            key = _get_api_key()
        except RuntimeError as e:
            logger.error(f"MemoryRetrieveNode 鉴权配置缺失: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(e),
                summary="鉴权未配置",
            )

        logger.info(f"MemoryRetrieveNode retrieve query={text!r} top_k={top_k}")
        try:
            async with httpx.AsyncClient(
                base_url=_get_base_url(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=timeout,
            ) as client:
                result = await _rpc(
                    client,
                    "retrieve",
                    {
                        "text": text,
                        "top_k": top_k,
                        "token_budget": token_budget,
                        "aggregate": aggregate,
                    },
                )
            blocks = result.get("blocks", []) if isinstance(result, dict) else []
            total_tokens = result.get("total_tokens", 0) if isinstance(result, dict) else 0
            logger.info(f"MemoryRetrieveNode retrieve ok: {len(blocks)} blocks, {total_tokens} tokens")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "context": result,
                    "total_tokens": total_tokens,
                    "block_count": len(blocks),
                },
                summary=f"召回 {len(blocks)} 块记忆 ({total_tokens} tokens)",
            )
        except Exception as e:
            logger.exception("MemoryRetrieveNode 执行异常")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"记忆检索异常: {e}",
                summary="执行异常",
            )
