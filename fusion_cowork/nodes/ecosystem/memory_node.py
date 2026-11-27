"""Fusion ecosystem interop nodes — bridges to the fusion-memory long-term memory hub.

Writes / retrieves cross-session memory through fm-server HTTP JSON-RPC
(127.0.0.1, Bearer auth). Offline hard constraint: HTTP only to the local
fusion-memory, never to the cloud (aligned with the fusion-memory 100%
offline constraint).

References: fusion-memory clients/python/fusion_memory_client.py (HTTP wire
contract) + clients/README.md (protocol matrix + env table).
"""

from __future__ import annotations

import json
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
        raise RuntimeError("FUSION_MEMORY_API_KEY 未配置 (fm-server Bearer 鉴权, B5)")  # i18n: user-facing
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
    """Memory commit node — writes workflow trajectory (TrajectoryRecorder output / SharedContext)
    into fusion-memory as an Interaction, returning turn-level memory_id list.

    Wires at the end of a workflow DAG, forming the *workflow output → long-term
    memory → next session recall* cross-session loop.
    """

    name = "memory_commit"
    display_name = "记忆提交"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "把工作流轨迹作为 Interaction 提交到 fusion-memory 长期记忆"  # i18n: user-facing
    icon = "🧠"
    default_label = "记忆提交"  # i18n: user-facing

    inputs = [
        # i18n: user-facing
        {"key": "interaction", "label": "Interaction 对象", "type": "object"},
        # i18n: user-facing
        {"key": "session_id", "label": "会话 ID", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "memory_ids", "label": "记忆 ID 列表", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "会话 ID (与 interaction.session_id 一致)",
                },
                "interaction": {
                    "type": "object",
                    # i18n: user-facing
                    "description": (
                        "Interaction 对象: {id, session_id, turns:[{turn_idx,"  # i18n: user-facing
                        "user_message, assistant_message, tool_calls}], timestamp, metadata}"
                    ),
                },
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
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
                # i18n: user-facing
                error="缺少必填参数 session_id 或 interaction",
                # i18n: user-facing
                summary="参数不完整",
            )

        try:
            key = _get_api_key()
        except RuntimeError as e:
            logger.error(f"MemoryCommitNode auth config missing: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(e),
                # i18n: user-facing
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
                # i18n: user-facing
                summary=f"提交 {len(memory_ids)} 条记忆",
            )
        except Exception as e:
            logger.exception("MemoryCommitNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"记忆提交异常: {e}",
                # i18n: user-facing
                summary="执行异常",
            )


@register_node
class MemoryRetrieveNode(BaseNode):
    """Memory retrieve node — calls retrieve_context by query, gets FormattedContext
    (blocks + total_tokens) to inject into downstream nodes' SharedContext.

    Wires at the front of a workflow DAG, providing cross-session memory
    context for subsequent nodes.
    """

    name = "memory_retrieve"
    display_name = "记忆检索"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "从 fusion-memory 检索跨 session 记忆上下文注入下游"  # i18n: user-facing
    icon = "🔍"
    default_label = "记忆检索"  # i18n: user-facing

    inputs = [
        # i18n: user-facing
        {"key": "text", "label": "查询文本", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "context", "label": "记忆上下文", "type": "object"},
        # i18n: user-facing
        {"key": "total_tokens", "label": "总 token 数", "type": "integer"},
        # i18n: user-facing
        {"key": "block_count", "label": "记忆块数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "查询文本 (语义检索)",
                },
                "top_k": {
                    "type": "integer",
                    # i18n: user-facing
                    "description": "召回数",
                    "default": 10,
                },
                "token_budget": {
                    "type": "integer",
                    # i18n: user-facing
                    "description": "token 预算上限",
                    "default": 4096,
                },
                "aggregate": {
                    "type": "boolean",
                    # i18n: user-facing
                    "description": "是否按 interaction 聚合 turns",
                    "default": True,
                },
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
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
                # i18n: user-facing
                error="缺少必填参数 text",
                # i18n: user-facing
                summary="参数不完整",
            )

        try:
            key = _get_api_key()
        except RuntimeError as e:
            logger.error(f"MemoryRetrieveNode auth config missing: {e}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(e),
                # i18n: user-facing
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
            # issue #554: artifact_scan on memory blocks before loading into the run — blocks hit by
            # injection/credential rules are dropped and quarantined (kept out of downstream SharedContext), the rest injected as usual
            quarantined = 0
            try:
                from ...engine.kernel_gates import get_quarantine, scan_or_quarantine

                kept = []
                for block in blocks:
                    text = json.dumps(block, ensure_ascii=False) if not isinstance(block, str) else block
                    clean, report = scan_or_quarantine(text, source=f"memory:{text[:40]!r}", kind="memory")
                    if clean:
                        kept.append(block)
                    else:
                        quarantined += 1
                        get_quarantine().add(report, preview=text[:200])
                if quarantined:
                    blocks = kept
                    total_tokens = max(0, total_tokens - quarantined)
                    logger.warning(f"MemoryRetrieveNode quarantined {quarantined} memory blocks that failed the scan")
            except Exception as e:
                # scan infrastructure failures do not block retrieval — log and pass through conservatively (fail-open only for infrastructure faults)
                logger.warning(f"MemoryRetrieveNode artifact scan failed (pass-through): {e}")
            logger.info(
                f"MemoryRetrieveNode retrieve ok: {len(blocks)} blocks, {total_tokens} tokens, quarantined={quarantined}"
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "context": result,
                    "total_tokens": total_tokens,
                    "block_count": len(blocks),
                },
                # i18n: user-facing
                summary=f"召回 {len(blocks)} 块记忆 ({total_tokens} tokens)"
                + (f", 隔离 {quarantined}" if quarantined else ""),  # i18n: user-facing
            )
        except Exception as e:
            logger.exception("MemoryRetrieveNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"记忆检索异常: {e}",
                # i18n: user-facing
                summary="执行异常",
            )
