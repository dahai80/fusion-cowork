# Run 级 fusion-memory 集成 — 启动召回注入 + 结束轨迹写回 (cowork issue #157 / fusion-memory#20)。
#
# Run 启动: retrieve 召回相关长期记忆, 经 initial_input["memory_context"] 注入首节点输入。
# Run 结束 (success): WorkflowExecution 步骤 + 结果构建 Interaction, commit 到 fusion-memory,
# 进入短期记忆, 由三线调度按复用晋升长期 (fusion-memory 引擎侧已实现)。
#
# 设计约束:
# - 零冲击降级: 未配 FUSION_MEMORY_API_KEY → from_env 返 None, 集成整体关闭, 行为与旧版一致。
# - 服务不可用不打断 run: 桥内异常一律 warn + None, 调用方记入 run record (fail-visible)。
# - 离线硬约束: 仅 HTTP 到本机 fm-server (默认 http://127.0.0.1:11435), 不连云。
#
# wire 契约对齐 fusion-memory clients/python/fusion_memory_client.py + fm-core interaction.rs:
# Interaction={id, session_id, tenant?, turns, timestamp(u64 epoch 秒), metadata};
# Turn={turn_idx, user_message, assistant_message, tool_calls}; ToolCall={name, args, result_summary};
# ContextBlock 文本字段为 turns_text (fm-core context.rs)。
# commit 结果契约: {"memory_ids": [...], "failed_turns": [...]} (fm-server P1-1)。

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:11435"
# 单字段内容上限: 节点输出可能巨大, 截断防炸记忆库嵌入
_FIELD_CAP = 4000
# tool_calls 步骤数上限: 大 DAG 全量序列化过长
_MAX_TOOL_CALLS = 64


def get_base_url() -> str:
    return os.environ.get("FUSION_MEMORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _compact(value: Any) -> str:
    # 任意输出压成可读短文本: dict/list 走 JSON, 其余 str()。失败兜底 repr。
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception as e:
        logger.warning(f"输出序列化失败, 用 repr 兜底: {e}")
        text = repr(value)
    return text[:_FIELD_CAP]


def _status_text(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


class MemoryBridge:
    # Run 级记忆桥。方法永不 raise — 失败降级返回 None, 由调用方记录。

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> Optional[MemoryBridge]:
        # 未配 FUSION_MEMORY_API_KEY → None, 集成整体关闭 (零冲击降级)。
        key = os.environ.get("FUSION_MEMORY_API_KEY", "").strip()
        if not key:
            logger.info("FUSION_MEMORY_API_KEY 未配置, run 级记忆集成关闭")
            return None
        return cls(get_base_url(), key)

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: Dict[str, Any]) -> Any:
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

    async def retrieve_context(self, query: str, top_k: int = 8, token_budget: int = 2048) -> Optional[str]:
        # Run 启动召回相关记忆, 返回拼接文本 (注入首节点输入)。失败/空 → None。
        if not query.strip():
            return None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                timeout=self._timeout,
            ) as client:
                result = await self._rpc(
                    client,
                    "retrieve",
                    {"text": query, "top_k": top_k, "token_budget": token_budget, "aggregate": True},
                )
        except Exception as e:
            logger.warning(f"memory retrieve 失败 (降级继续): {e}")
            return None
        blocks = result.get("blocks", []) if isinstance(result, dict) else []
        lines: List[str] = []
        for b in blocks:
            text = str(b.get("turns_text", "")).strip() if isinstance(b, dict) else str(b)
            if text:
                lines.append(text)
        if not lines:
            logger.info("memory retrieve 空结果 (首次运行属预期)")
            return None
        logger.info(f"memory retrieve 召回 {len(lines)} 块 (total_tokens={result.get('total_tokens', 0)})")
        return "\n\n".join(lines)[:_FIELD_CAP]

    @staticmethod
    def build_interaction(
        session_id: str,
        workflow_id: str,
        workflow_name: str,
        description: str,
        steps: List[Any],
        result_summary: str,
        error: str = "",
        execution_id: str = "",
    ) -> Dict[str, Any]:
        # WorkflowExecution → fm-core Interaction。单 turn: user=任务, assistant=结果, tool_calls=步骤。
        tool_calls: List[Dict[str, Any]] = []
        for s in steps[:_MAX_TOOL_CALLS]:
            name = getattr(s, "node_display_name", "") or getattr(s, "node_name", "") or getattr(s, "node_id", "")
            status = _status_text(getattr(s, "status", ""))
            summary = getattr(s, "summary", "") or getattr(s, "error", "") or ""
            if not summary:
                summary = _compact(getattr(s, "output_data", None))
            tool_calls.append({"name": name, "args": {}, "result_summary": f"[{status}] {summary}"[:_FIELD_CAP]})
        user_message = f"{workflow_name}: {description}".strip(": ")
        assistant_message = (result_summary or error or "(无输出)")[:_FIELD_CAP]
        return {
            "id": f"cowork_{uuid.uuid4().hex[:12]}",
            "session_id": session_id,
            "turns": [
                {
                    "turn_idx": 0,
                    "user_message": user_message[:_FIELD_CAP],
                    "assistant_message": assistant_message,
                    "tool_calls": tool_calls,
                }
            ],
            "timestamp": int(time.time()),
            "metadata": {
                "agent_type": "cowork",
                "node_id": workflow_id or execution_id,
                "tool_names": [tc["name"] for tc in tool_calls],
            },
        }

    async def write_back(self, interaction: Dict[str, Any]) -> Optional[List[str]]:
        # Run 结束写回。失败 → warn + None (run 已终态, 不重试; 召回不到属预期降级)。
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                timeout=self._timeout,
            ) as client:
                result = await self._rpc(
                    client,
                    "commit",
                    {"session_id": interaction["session_id"], "interaction": interaction},
                )
        except Exception as e:
            logger.warning(f"memory commit 失败 (降级): {e}")
            return None
        if isinstance(result, dict):
            ids = result.get("memory_ids", [])
            failed = result.get("failed_turns", [])
            if failed:
                logger.warning(f"memory commit 部分 turn 失败: {failed}")
        elif isinstance(result, list):
            # 旧契约兼容: 纯数组
            ids = result
        else:
            ids = []
        logger.info(f"memory commit ok: {len(ids)} ids (session={interaction.get('session_id')})")
        return ids
