"""Interactive conversation Agent Loop — Cowork positioning #1.

Conversational agent loop: user plain-language input → LLM step-by-step decisions (RUN_NODE/REPLY/ASK/DONE)
→ execute node → observe result → decide again; can be interrupted mid-way / supplemented with one more line and resumed.

Degradation: when LLM is unavailable, return a degraded explanation turn, do not execute nodes.
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
