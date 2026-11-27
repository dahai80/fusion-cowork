"""Interactive conversational Agent Loop - Cowork positioning #1.

Conversational agent loop: user plain-language input -> LLM decides step by step
(RUN_NODE/REPLY/ASK/DONE) -> execute node -> observe result -> decide again;
can be interrupted mid-run or resumed with a supplement message.

Degraded: when the LLM is unavailable, returns a degraded-explanation turn
without executing nodes.
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
