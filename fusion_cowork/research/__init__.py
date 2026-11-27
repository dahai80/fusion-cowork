"""Deep research package - P2-6.

Multi-agent research: plan (decompose the question) -> search (parallel web
searches) -> synthesize (LLM aggregates into a cited report). Falls back to
structured raw findings when the LLM is unavailable.
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
