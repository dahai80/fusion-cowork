"""Deep research package - P2-6.

Multi-agent research: planning (decompose question) -> search (parallel web search) -> synthesis (LLM-summarized report with citations).
Degrades to structured raw findings when LLM is unavailable.
"""

from .deep_research import (
    ResearchFinding,
    ResearchReport,
    ResearchSubQuestion,
    run_deep_research,
)

__all__ = [
    "ResearchFinding",
    "ResearchReport",
    "ResearchSubQuestion",
    "run_deep_research",
]
