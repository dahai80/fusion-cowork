"""Agent Loop 核心实现 — 对话式自主决策循环。

流程:
  user 输入 → LLM 决策单步动作 (RUN_NODE/REPLY/ASK/DONE)
  → RUN_NODE 时执行 NodeRegistry 节点, 观察结果回灌历史
  → 继续决策, 直到 DONE 或用户叫停
  → 中途可补充一句 (supplement) 注入下一轮决策

降级: LLM 不可用 → 返回降级说明 turn, degraded=True, 不执行节点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 数据结构 ──


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
    "你是 Fusion-Cowork 桌面自动化助手。每轮必须决策一个动作并输出 JSON:\n"
    '{"type":"RUN_NODE","node":"节点名或中文别名","params":{...},"message":"说明"} '
    "执行一个节点; 或\n"
    '{"type":"REPLY","message":"回复用户"} 直接回复; 或\n'
    '{"type":"ASK","message":"向用户提问"} 需要用户补充信息; 或\n'
    '{"type":"DONE","message":"任务完成总结"} 结束。\n'
    "可用节点由系统注入。只输出 JSON, 不要多余文字。"
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
        # R-2: 在途 node.execute handle, interrupt() 可真取消 (旧版仅翻 flag, 节点继续跑)
        self._exec_handle: Optional[asyncio.Task] = None
        # R-2: 单节点执行超时 (秒), 0=不限; 默认 60s 防 shell/REPL 卡死整条 loop
        self._node_timeout: float = 60.0
        self._supplement_queue: asyncio.Queue[str] = asyncio.Queue()
        self._available_nodes = self._collect_node_catalog()
        logger.debug(f"AgentLoop 初始化, 可用节点 {len(self._available_nodes)} 个")

    def _collect_node_catalog(self) -> str:
        try:
            from ..engine.node import NodeRegistry

            nodes = NodeRegistry.list()
            lines = []
            for n in nodes:
                params = n.get("params_schema", {}).get("properties", {})
                pstr = ", ".join(params.keys()) if params else "无"
                lines.append(f"- {n['name']} (别名可解析): {n['description']} [参数: {pstr}]")
            return "\n".join(lines) or "(无已注册节点)"
        except Exception as e:
            logger.warning(f"收集节点目录失败: {e}")
            return "(节点目录不可用)"

    async def run(self, user_input: str) -> AgentLoopResult:
        result = AgentLoopResult()
        # R-1: run 起始重置历史为仅系统提示, 防跨 run 累积无界增长 (旧版 append 永不清)
        self._messages = [{"role": "system", "content": self._system_prompt}]
        self._interrupted = False
        self._exec_handle = None
        self._messages.append({"role": "user", "content": user_input})
        result.turns.append(AgentTurn(role="user", content=user_input))

        resolved_model = await self._resolve_model()
        logger.info(f"AgentLoop 启动, model={resolved_model}, max_steps={self._max_steps}")

        for step in range(self._max_steps):
            if self._interrupted:
                result.interrupted = True
                logger.info("AgentLoop 被用户叫停")
                break

            supplement = await self._drain_supplement()
            if supplement:
                self._messages.append({"role": "user", "content": f"[用户补充] {supplement}"})
                result.turns.append(AgentTurn(role="user", content=f"[补充] {supplement}"))
                logger.info(f"注入用户补充: {supplement[:60]}")

            action = await self._decide(resolved_model)
            if action is None:
                result.degraded = True
                result.error = "LLM 决策失败 (降级)"
                result.turns.append(AgentTurn(role="assistant", content="决策失败, LLM 不可用, 进入降级模式。"))
                break

            turn = AgentTurn(role="assistant", content=action.message, action=action)
            result.turns.append(turn)
            self._messages.append(
                {"role": "assistant", "content": json.dumps(_action_to_dict(action), ensure_ascii=False)}
            )

            if action.type == "DONE":
                result.completed = True
                logger.info(f"AgentLoop 完成 (step {step}): {action.message[:80]}")
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
                self._messages.append({"role": "system", "content": f"[节点观察] {observation}"})
                if self._on_turn:
                    self._on_turn(turn)
            else:
                logger.warning(f"未知 action type: {action.type}, 当作 REPLY")
                continue
        else:
            logger.warning(f"AgentLoop 达到 max_steps={self._max_steps}, 停止")
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
            logger.debug(f"list_models 失败: {e}")
        return "qwen3.5-9b"

    async def _decide(self, model: str) -> Optional[AgentAction]:
        prompt_messages = list(self._messages)
        if not any("[可用节点]" in m["content"] for m in prompt_messages if m["role"] == "system"):
            prompt_messages.append({"role": "system", "content": f"[可用节点]\n{self._available_nodes}"})
        try:
            resp = await self._client.chat(model, prompt_messages, temperature=0.3, max_tokens=1024)
            return _parse_action(resp.content.strip())
        except Exception as e:
            logger.warning(f"LLM 决策调用失败: {e}")
            return None

    async def _execute_node(self, action: AgentAction) -> str:
        try:
            from ..engine.node import NodeConfig, NodeRegistry

            backend = NodeRegistry.resolve_alias(action.node)
            node = NodeRegistry.create(backend, config=NodeConfig(params=action.params))
            if node is None:
                return f"节点 '{action.node}' (→{backend}) 不存在"
            logger.info(f"执行节点 {backend}, params={action.params}")
            # R-2: node.execute 无超时 → 卡死循环; 存 handle 供 interrupt 真取消
            self._exec_handle = asyncio.ensure_future(node.execute({}))
            try:
                r = await asyncio.wait_for(asyncio.shield(self._exec_handle), timeout=self._node_timeout)
            finally:
                self._exec_handle = None
            obs = {
                "status": r.status.value if r.status else "unknown",
                "summary": r.summary or "",
                "error": r.error or "",
                "data": (r.data or {}) if len(str(r.data or {})) < 2000 else "(数据过大已省略)",
            }
            return json.dumps(obs, ensure_ascii=False)
        except TimeoutError:
            logger.warning(f"节点执行超时 {action.node} ({self._node_timeout}s)")
            return f"节点执行超时: {action.node}"
        except asyncio.CancelledError:
            logger.info(f"节点执行被中断: {action.node}")
            raise
        except Exception as e:
            logger.error(f"节点执行异常 {action.node}: {e}", exc_info=True)
            return f"节点执行异常: {e}"

    async def _drain_supplement(self) -> str:
        try:
            return await asyncio.wait_for(self._supplement_queue.get(), timeout=0.01)
        except TimeoutError:
            return ""
        except Exception:
            return ""

    def interrupt(self) -> None:
        self._interrupted = True
        # R-2: 真取消在途 node.execute 协程 (旧版仅翻 flag, 当前节点跑完才停, 用户叫停无效)
        if self._exec_handle and not self._exec_handle.done():
            self._exec_handle.cancel()
            logger.info(f"已取消在途节点执行: handle={id(self._exec_handle)}")
        logger.info("收到中断信号")

    async def supplement(self, message: str) -> None:
        await self._supplement_queue.put(message)
        logger.info(f"补充消息入队: {message[:60]}")


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
            logger.warning(f"无法解析 action JSON: {raw[:120]}")
            return AgentAction(type="REPLY", message=raw.strip()[:500])
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning(f"action JSON 提取失败: {raw[:120]}")
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
