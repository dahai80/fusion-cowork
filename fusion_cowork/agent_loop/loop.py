"""Agent Loop core implementation — conversational autonomous decision loop.

Flow:
  user input → LLM decides a single-step action (RUN_NODE/REPLY/ASK/DONE)
  → on RUN_NODE, execute the NodeRegistry node, feed the observation back into history
  → keep deciding until DONE or user interruption
  → mid-way supplement (supplement) is injected into the next decision round

Degradation: LLM unavailable → return a degraded explanation turn, degraded=True, no node execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Data structures ──


@dataclass
class AgentAction:
    type: str = "REPLY"
    node: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass
class AgentTurn:
    role: str = "user"
    content: str = ""
    action: Optional[AgentAction] = None
    observation: str = ""


@dataclass
class AgentLoopResult:
    turns: List[AgentTurn] = field(default_factory=list)
    completed: bool = False
    interrupted: bool = False
    degraded: bool = False
    error: str = ""


# ── AgentLoop ──


DEFAULT_SYSTEM_PROMPT = (
    "You are the Fusion-Cowork desktop automation assistant. Each round you must decide one action and output JSON:\n"
    '{"type":"RUN_NODE","node":"node name or Chinese alias","params":{...},"message":"explanation"} '
    "to execute a node; or\n"
    '{"type":"REPLY","message":"reply to the user"} to reply directly; or\n'
    '{"type":"ASK","message":"ask the user a question"} when more user input is needed; or\n'
    '{"type":"DONE","message":"task completion summary"} to finish.\n'
    "Available nodes are injected by the system. Output JSON only, no extra text."
)


class AgentLoop:
    def __init__(
        self,
        mlx_client: Any = None,
        model: str = "",
        max_steps: int = 20,
        system_prompt: str = "",
        on_turn: Optional[Callable[[AgentTurn], None]] = None,
    ):
        self._client = mlx_client
        if self._client is None:
            from ..ai.mlx_client import FusionMLXClient

            self._client = FusionMLXClient()
        self._model = model
        self._max_steps = max_steps
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._on_turn = on_turn
        self._messages: List[Dict[str, str]] = [{"role": "system", "content": self._system_prompt}]
        self._interrupted = False
        # R-2: in-flight node.execute handle, interrupt() can truly cancel it (old version only flipped a flag, node kept running)
        self._exec_handle: Optional[asyncio.Task] = None
        # R-2: per-node execution timeout (seconds), 0=unlimited; default 60s prevents shell/REPL from hanging the whole loop
        self._node_timeout: float = 60.0
        self._supplement_queue: asyncio.Queue[str] = asyncio.Queue()
        self._available_nodes = self._collect_node_catalog()
        logger.debug(f"AgentLoop initialized, {len(self._available_nodes)} available nodes")

    def _collect_node_catalog(self) -> str:
        try:
            from ..engine.node import NodeRegistry

            nodes = NodeRegistry.list()
            lines = []
            for n in nodes:
                params = n.get("params_schema", {}).get("properties", {})
                pstr = ", ".join(params.keys()) if params else "none"
                lines.append(f"- {n['name']} (alias-resolvable): {n['description']} [params: {pstr}]")
            return "\n".join(lines) or "(no registered nodes)"
        except Exception as e:
            logger.warning(f"Failed to collect node catalog: {e}")
            return "(node catalog unavailable)"

    async def run(self, user_input: str) -> AgentLoopResult:
        result = AgentLoopResult()
        # R-1: reset history to system prompt only at run start, prevent unbounded cross-run growth (old version appended and never cleared)
        self._messages = [{"role": "system", "content": self._system_prompt}]
        self._interrupted = False
        self._exec_handle = None
        self._messages.append({"role": "user", "content": user_input})
        result.turns.append(AgentTurn(role="user", content=user_input))

        resolved_model = await self._resolve_model()
        logger.info(f"AgentLoop started, model={resolved_model}, max_steps={self._max_steps}")

        for step in range(self._max_steps):
            if self._interrupted:
                result.interrupted = True
                logger.info("AgentLoop interrupted by user")
                break

            supplement = await self._drain_supplement()
            if supplement:
                self._messages.append({"role": "user", "content": f"[User supplement] {supplement}"})
                # i18n: user-facing
                result.turns.append(AgentTurn(role="user", content=f"[补充] {supplement}"))
                logger.info(f"Injected user supplement: {supplement[:60]}")

            action = await self._decide(resolved_model)
            if action is None:
                result.degraded = True
                result.error = "LLM decision failed (degraded)"
                # i18n: user-facing
                result.turns.append(AgentTurn(role="assistant", content="决策失败, LLM 不可用, 进入降级模式。"))
                break

            turn = AgentTurn(role="assistant", content=action.message, action=action)
            result.turns.append(turn)
            self._messages.append(
                {"role": "assistant", "content": json.dumps(_action_to_dict(action), ensure_ascii=False)}
            )

            if action.type == "DONE":
                result.completed = True
                logger.info(f"AgentLoop completed (step {step}): {action.message[:80]}")
                break
            if action.type in ("REPLY", "ASK"):
                if self._on_turn:
                    self._on_turn(turn)
                if action.type == "ASK":
                    break
                continue

            if action.type == "RUN_NODE":
                observation = await self._execute_node(action)
                turn.observation = observation
                self._messages.append({"role": "system", "content": f"[Node observation] {observation}"})
                if self._on_turn:
                    self._on_turn(turn)
            else:
                logger.warning(f"Unknown action type: {action.type}, treating as REPLY")
                continue
        else:
            logger.warning(f"AgentLoop reached max_steps={self._max_steps}, stopping")
            # i18n: user-facing
            result.turns.append(AgentTurn(role="assistant", content=f"达到最大步数 {self._max_steps}, 循环停止。"))

        return result

    async def _resolve_model(self) -> str:
        if self._model:
            return self._model
        try:
            models = await self._client.list_models()
            if models:
                mid = models[0].get("id", models[0].get("model", ""))
                if mid:
                    return mid
        except Exception as e:
            logger.debug(f"list_models failed: {e}")
        return "qwen3.5-9b"

    async def _decide(self, model: str) -> Optional[AgentAction]:
        prompt_messages = list(self._messages)
        if not any("[Available nodes]" in m["content"] for m in prompt_messages if m["role"] == "system"):
            prompt_messages.append({"role": "system", "content": f"[Available nodes]\n{self._available_nodes}"})
        try:
            resp = await self._client.chat(model, prompt_messages, temperature=0.3, max_tokens=1024)
            return _parse_action(resp.content.strip())
        except Exception as e:
            logger.warning(f"LLM decision call failed: {e}")
            return None

    async def _execute_node(self, action: AgentAction) -> str:
        try:
            from ..engine.node import NodeConfig, NodeRegistry

            backend = NodeRegistry.resolve_alias(action.node)
            node = NodeRegistry.create(backend, config=NodeConfig(params=action.params))
            if node is None:
                return f"Node '{action.node}' (→{backend}) does not exist"
            logger.info(f"Executing node {backend}, params={action.params}")
            # R-2: node.execute has no timeout → loop hangs; store handle so interrupt can truly cancel
            self._exec_handle = asyncio.ensure_future(node.execute({}))
            try:
                r = await asyncio.wait_for(asyncio.shield(self._exec_handle), timeout=self._node_timeout)
            finally:
                self._exec_handle = None
            obs = {
                "status": r.status.value if r.status else "unknown",
                "summary": r.summary or "",
                "error": r.error or "",
                "data": (r.data or {}) if len(str(r.data or {})) < 2000 else "(data too large, omitted)",
            }
            return json.dumps(obs, ensure_ascii=False)
        except TimeoutError:
            logger.warning(f"Node execution timed out {action.node} ({self._node_timeout}s)")
            return f"Node execution timed out: {action.node}"
        except asyncio.CancelledError:
            logger.info(f"Node execution interrupted: {action.node}")
            raise
        except Exception as e:
            logger.error(f"Node execution error {action.node}: {e}", exc_info=True)
            return f"Node execution error: {e}"

    async def _drain_supplement(self) -> str:
        try:
            return await asyncio.wait_for(self._supplement_queue.get(), timeout=0.01)
        except TimeoutError:
            return ""
        except Exception:
            return ""

    def interrupt(self) -> None:
        self._interrupted = True
        # R-2: truly cancel the in-flight node.execute coroutine (old version only flipped a flag, current node ran to completion before stopping, user interrupt had no effect)
        if self._exec_handle and not self._exec_handle.done():
            self._exec_handle.cancel()
            logger.info(f"Cancelled in-flight node execution: handle={id(self._exec_handle)}")
        logger.info("Interrupt signal received")

    async def supplement(self, message: str) -> None:
        await self._supplement_queue.put(message)
        logger.info(f"Supplement message queued: {message[:60]}")


async def run_agent_loop(
    user_input: str,
    *,
    model: str = "",
    mlx_client: Any = None,
    max_steps: int = 20,
) -> AgentLoopResult:
    loop = AgentLoop(mlx_client=mlx_client, model=model, max_steps=max_steps)
    return await loop.run(user_input)


def _action_to_dict(action: AgentAction) -> Dict[str, Any]:
    return {
        "type": action.type,
        "node": action.node,
        "params": action.params,
        "message": action.message,
    }


def _parse_action(raw: str) -> Optional[AgentAction]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning(f"Cannot parse action JSON: {raw[:120]}")
            return AgentAction(type="REPLY", message=raw.strip()[:500])
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning(f"Action JSON extraction failed: {raw[:120]}")
            return AgentAction(type="REPLY", message=raw.strip()[:500])
    if not isinstance(data, dict):
        return AgentAction(type="REPLY", message=str(raw)[:500])
    atype = str(data.get("type", "REPLY")).upper()
    if atype not in ("RUN_NODE", "REPLY", "ASK", "DONE"):
        atype = "REPLY"
    params = data.get("params", {})
    if not isinstance(params, dict):
        params = {}
    return AgentAction(
        type=atype,
        node=str(data.get("node", "")),
        params=params,
        message=str(data.get("message", "")),
    )
