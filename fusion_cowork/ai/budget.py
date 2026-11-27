"""Token budget and cost accounting — parity with Cowork --max-budget-usd.

Accumulates LLM call token usage and USD cost; rejects further calls once the budget cap is reached.
Local MLX inference cost is near zero, but the cost accounting framework is kept for Claude Cowork parity.
Covers audit P2-2: missing token budget/cost accounting.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# USD per 1M tokens — local MLX is near-zero cost, framework kept for parity
_DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    "default": {"input": 0.0, "output": 0.0},
}


@dataclass
class BudgetRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


class BudgetTracker:
    """Per-session token budget tracker.

    With enforce=False nothing is blocked (accounting only); with enforce=True and
    max_budget_usd>0, once accumulated cost exceeds the cap record() returns False
    and the caller should abort generation.
    """

    _lock = threading.Lock()

    def __init__(
        self, max_budget_usd: float = 0.0, enforce: bool = False, pricing: Optional[Dict[str, Dict[str, float]]] = None
    ):
        self._max_budget_usd = float(max_budget_usd)
        self._enforce = enforce and self._max_budget_usd > 0
        self._pricing = pricing or _DEFAULT_PRICING
        self._record = BudgetRecord()
        if self._enforce:
            logger.info(f"BudgetTracker enabled: cap ${self._max_budget_usd:.4f}")

    @property
    def enforce(self) -> bool:
        return self._enforce

    @property
    def max_budget_usd(self) -> float:
        return self._max_budget_usd

    @property
    def record(self) -> BudgetRecord:
        return self._record

    def remaining_usd(self) -> float:
        return max(0.0, self._max_budget_usd - self._record.cost_usd)

    def is_exceeded(self, graph_id: str = "") -> bool:
        """Kernel funnel budget contract (fusion-harness funnels._budget_exceeded).

        graph_id accepted for call-form parity (`is_exceeded(graph_id)`); the
        tracker is session-scoped, so the graph dimension is not tracked here.
        """
        return self._enforce and self._record.cost_usd >= self._max_budget_usd

    @property
    def spent_tokens(self) -> int:
        """Tokens consumed so far — funnel contract reads this via getattr
        without calling it, so it must be an attribute, not a method."""
        return self._record.total_tokens

    def record_usage(self, usage: Dict, model: str = "default") -> bool:
        """Record one call's usage, return whether still within budget.

        usage must contain prompt_tokens/completion_tokens (OpenAI format).
        """
        prompt_t = int(usage.get("prompt_tokens", 0))
        completion_t = int(usage.get("completion_tokens", 0))
        total_t = int(usage.get("total_tokens", prompt_t + completion_t))
        price = self._pricing.get(model, self._pricing["default"])
        cost = (prompt_t / 1_000_000.0) * price.get("input", 0.0) + (completion_t / 1_000_000.0) * price.get(
            "output", 0.0
        )
        with self._lock:
            self._record.prompt_tokens += prompt_t
            self._record.completion_tokens += completion_t
            self._record.total_tokens += total_t
            self._record.cost_usd += cost
            self._record.calls += 1
            over = self._enforce and self._record.cost_usd >= self._max_budget_usd
        if over:
            logger.warning(
                f"Budget exceeded: ${self._record.cost_usd:.6f} >= ${self._max_budget_usd:.4f} (calls={self._record.calls})"
            )
            return False
        logger.debug(
            f"Budget accounting: +{total_t}t +${cost:.6f} (total ${self._record.cost_usd:.6f}/{self._max_budget_usd:.4f})"
        )
        return True

    def to_dict(self) -> Dict:
        r = self._record
        return {
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "cost_usd": round(r.cost_usd, 6),
            "calls": r.calls,
            "max_budget_usd": self._max_budget_usd,
            "remaining_usd": round(self.remaining_usd(), 6),
            "enforce": self._enforce,
        }


_default_tracker: Optional[BudgetTracker] = None


def get_budget_tracker(max_budget_usd: float = 0.0, enforce: bool = False) -> BudgetTracker:
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = BudgetTracker(max_budget_usd=max_budget_usd, enforce=enforce)
    return _default_tracker


def reset_budget_tracker() -> None:
    global _default_tracker
    _default_tracker = None
