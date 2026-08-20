"""Token 预算与成本记账 — 对标 Cowork --max-budget-usd。

累积 LLM 调用 token 用量与 USD 成本, 达到预算上限拒绝后续调用。
本地 MLX 推理成本极低, 但保留成本记账框架以对标 Claude Cowork。
对应审计 P2-2: token 预算/成本记账缺失。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# USD per 1M tokens — 本地 MLX 近零成本, 保留框架对标
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
    """单次会话 token 预算追踪器。

    enforce=False 时不拦截 (仅记账); enforce=True 且 max_budget_usd>0 时
    累计成本超限则 record() 返回 False, 调用方应中止生成。
    """

    _lock = threading.Lock()

    def __init__(self, max_budget_usd: float = 0.0, enforce: bool = False, pricing: Optional[Dict[str, Dict[str, float]]] = None):
        self._max_budget_usd = float(max_budget_usd)
        self._enforce = enforce and self._max_budget_usd > 0
        self._pricing = pricing or _DEFAULT_PRICING
        self._record = BudgetRecord()
        if self._enforce:
            logger.info(f"BudgetTracker 启用: 上限 ${self._max_budget_usd:.4f}")

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

    def record_usage(self, usage: Dict, model: str = "default") -> bool:
        """记录一次调用用量, 返回是否仍在预算内。

        usage 需含 prompt_tokens/completion_tokens (OpenAI 格式)。
        """
        prompt_t = int(usage.get("prompt_tokens", 0))
        completion_t = int(usage.get("completion_tokens", 0))
        total_t = int(usage.get("total_tokens", prompt_t + completion_t))
        price = self._pricing.get(model, self._pricing["default"])
        cost = (prompt_t / 1_000_000.0) * price.get("input", 0.0) + (completion_t / 1_000_000.0) * price.get("output", 0.0)
        with self._lock:
            self._record.prompt_tokens += prompt_t
            self._record.completion_tokens += completion_t
            self._record.total_tokens += total_t
            self._record.cost_usd += cost
            self._record.calls += 1
            over = self._enforce and self._record.cost_usd >= self._max_budget_usd
        if over:
            logger.warning(f"预算超限: ${self._record.cost_usd:.6f} >= ${self._max_budget_usd:.4f} (calls={self._record.calls})")
            return False
        logger.debug(f"预算记账: +{total_t}t +${cost:.6f} (累计 ${self._record.cost_usd:.6f}/{self._max_budget_usd:.4f})")
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
