"""交互式对话 Agent Loop — Cowork 定位 #1。

对话式 agent loop: 用户白话 → LLM 逐步决策 (RUN_NODE/REPLY/ASK/DONE)
→ 执行节点 → 观察结果 → 再决策; 中途可叫停/补一句再续。

降级: LLM 不可用时返回降级说明, 不执行节点。
"""

from .loop import (
    AgentAction,
    AgentLoop,
    AgentTurn,
    run_agent_loop,
)

__all__ = [
    "AgentAction",
    "AgentLoop",
    "AgentTurn",
    "run_agent_loop",
]
