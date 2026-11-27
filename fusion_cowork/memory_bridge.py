# Run-level fusion-memory integration — startup recall injection + end-of-run trajectory write-back (cowork issue #157 / fusion-memory#20).
#
# Run start: retrieve relevant long-term memories, injected into the first node's input via
# initial_input["memory_context"].
# Run end (success): the WorkflowExecution steps + result build an Interaction, committed to
# fusion-memory, enters short-term memory, promoted to long-term by the three-tier scheduler
# based on reuse (already implemented on the fusion-memory engine side).
#
# Design constraints:
# - Zero-impact degradation: without FUSION_MEMORY_API_KEY, from_env returns None, the whole
#   integration is off, behavior identical to the previous version.
# - Service unavailability never breaks a run: bridge exceptions always warn + None; the caller
#   records it in the run record (fail-visible).
# - Offline hard constraint: HTTP only to the local fm-server (default http://127.0.0.1:11435), never cloud.
#
# Wire contract aligned with fusion-memory clients/python/fusion_memory_client.py + fm-core interaction.rs:
# Interaction={id, session_id, tenant?, turns, timestamp(u64 epoch seconds), metadata};
# Turn={turn_idx, user_message, assistant_message, tool_calls}; ToolCall={name, args, result_summary};
# ContextBlock text field is turns_text (fm-core context.rs).
# commit result contract: {"memory_ids": [...], "failed_turns": [...]} (fm-server P1-1).

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
# Per-field content cap: node outputs can be huge, truncated to avoid blowing up memory-store embeddings
_FIELD_CAP = 4000
# tool_calls step cap: fully serializing a large DAG is too long
_MAX_TOOL_CALLS = 64


def get_base_url() -> str:
    return os.environ.get("FUSION_MEMORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _compact(value: Any) -> str:
    # Compact any output into readable short text: dict/list via JSON, everything else str(). Falls back to repr on failure.
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception as e:
        logger.warning(f"Output serialization failed, falling back to repr: {e}")
        text = repr(value)
    return text[:_FIELD_CAP]


def _status_text(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


class MemoryBridge:
    # Run-level memory bridge. Methods never raise — failures degrade to None, caller records.

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> Optional[MemoryBridge]:
        # Without FUSION_MEMORY_API_KEY -> None, the whole integration is off (zero-impact degradation).
        key = os.environ.get("FUSION_MEMORY_API_KEY", "").strip()
        if not key:
            logger.info("FUSION_MEMORY_API_KEY not configured, run-level memory integration disabled")
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
        # Run start: recall relevant memories, return the joined text (injected into the first node input). Failure/empty -> None.
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
            logger.warning(f"memory retrieve failed (degrading, continuing): {e}")
            return None
        blocks = result.get("blocks", []) if isinstance(result, dict) else []
        lines: List[str] = []
        for b in blocks:
            text = str(b.get("turns_text", "")).strip() if isinstance(b, dict) else str(b)
            if text:
                lines.append(text)
        if not lines:
            logger.info("memory retrieve empty result (expected on first run)")
            return None
        logger.info(f"memory retrieve recalled {len(lines)} blocks (total_tokens={result.get('total_tokens', 0)})")
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
        # WorkflowExecution -> fm-core Interaction. Single turn: user=task, assistant=result, tool_calls=steps.
        tool_calls: List[Dict[str, Any]] = []
        for s in steps[:_MAX_TOOL_CALLS]:
            name = getattr(s, "node_display_name", "") or getattr(s, "node_name", "") or getattr(s, "node_id", "")
            status = _status_text(getattr(s, "status", ""))
            summary = getattr(s, "summary", "") or getattr(s, "error", "") or ""
            if not summary:
                summary = _compact(getattr(s, "output_data", None))
            tool_calls.append({"name": name, "args": {}, "result_summary": f"[{status}] {summary}"[:_FIELD_CAP]})
        user_message = f"{workflow_name}: {description}".strip(": ")
        assistant_message = (result_summary or error or "(无输出)")[:_FIELD_CAP]  # i18n: user-facing
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
        # Run end write-back. Failure -> warn + None (run already terminal, no retry; empty recall is an expected degradation).
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
            logger.warning(f"memory commit failed (degraded): {e}")
            return None
        if isinstance(result, dict):
            ids = result.get("memory_ids", [])
            failed = result.get("failed_turns", [])
            if failed:
                logger.warning(f"memory commit partially failed turns: {failed}")
        elif isinstance(result, list):
            # Legacy contract compatibility: plain array
            ids = result
        else:
            ids = []
        logger.info(f"memory commit ok: {len(ids)} ids (session={interaction.get('session_id')})")
        return ids
